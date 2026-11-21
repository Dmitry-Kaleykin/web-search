import { spawn } from "node:child_process";
import { access, readFile } from "node:fs/promises";
import { constants } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  Key,
  ProcessTerminal,
  ScrollView,
  SelectList,
  Text,
  TuiAltScreen,
  VStack,
  matchesKey,
  stripTerminalSequences,
} from "@earendil-works/pi-tui";
import { createServices } from "./services.js";
import { copyText } from "./clipboard.js";
import { createSessionLog } from "./diagnostics.js";

const UI_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const PROJECT_DIR = path.resolve(UI_DIR, "..");
const EVALUATOR = path.join(PROJECT_DIR, ".venv", "bin", "web-search-eval");
const sessionLog = createSessionLog(PROJECT_DIR);
const plainCheck = process.argv.includes("--check");

const ansi = {
  accent: (text) => `\x1b[38;5;75m${text}\x1b[0m`,
  dim: (text) => `\x1b[2m${text}\x1b[0m`,
  success: (text) => `\x1b[38;5;78m${text}\x1b[0m`,
  warning: (text) => `\x1b[38;5;214m${text}\x1b[0m`,
  error: (text) => `\x1b[38;5;203m${text}\x1b[0m`,
  bold: (text) => `\x1b[1m${text}\x1b[0m`,
};

const actions = [
  {
    value: "rebuild",
    label: "Rebuild isolated runtime",
    description: "Apply project changes and verify isolation; restart Pi afterward",
  },
  {
    value: "setup",
    label: "Install / update and start",
    description: "Install dependencies, build the isolated runtime, and prepare Docker",
  },
  {
    value: "start",
    label: "Start services",
    description: "Prepare isolated search; Pi starts its own worker when it connects",
  },
  {
    value: "stop",
    label: "Stop search workers",
    description: "Disconnect Pi’s search workers; reconnect Pi to search again",
  },
  {
    value: "doctor",
    label: "Run readiness checks",
    description: "Verify isolation, public-page reading, Chromium, and a real search",
  },
  {
    value: "evaluation",
    label: "Test search and reading (offline)",
    description: "Check the public tools with fixtures; no network or model calls",
  },
  {
    value: "logs",
    label: "Toggle live logs",
    description: "Follow the network broker’s domain-only connection records",
  },
  {
    value: "refresh",
    label: "Refresh status",
    description: "Recheck Docker, the installed runtime, and Pi’s search workers",
  },
  { value: "quit", label: "Quit", description: "Leave services in their current state" },
];

const terminal = new ProcessTerminal();
const tui = new TuiAltScreen(terminal, false, undefined, {
  mouse: true,
  copySelection: (text) => copyText(text),
  searchMatchStyle: (text) => `\x1b[30;43m${text}\x1b[0m`,
  searchCurrentMatchStyle: (text) => `\x1b[30;46m${text}\x1b[0m`,
});

const header = new Text(
  `${ansi.accent(ansi.bold("Local Agentic Web Search"))}\n${ansi.dim(
    "Isolated search · Pi starts its worker when it connects",
  )}`,
  1,
  0,
);
const statusText = new Text(ansi.dim("Checking local services…"), 1, 1);
const activityText = new Text(ansi.dim("Ready"), 1, 0);
const logText = new Text(ansi.dim("No logs yet."), 1, 0);
const logScroll = new ScrollView(logText, {
  follow: "end",
  primary: true,
  overscroll: "contain",
  scrollbar: "auto",
  scrollbarStyle: (text) => ansi.dim(text),
});
const menu = new SelectList(actions, actions.length, {
  selectedPrefix: (text) => ansi.accent(text),
  selectedText: (text) => ansi.accent(ansi.bold(text)),
  description: (text) => ansi.dim(text),
  scrollInfo: (text) => ansi.dim(text),
  noMatch: (text) => ansi.warning(text),
});
const footer = new Text(
  ansi.dim("↑↓ choose · enter run · esc/q quit · ctrl+y copy logs · ctrl+o open log · ctrl+shift+f search"),
  1,
  0,
);

const layout = new VStack(
  [
    { component: header, basis: "auto" },
    { component: statusText, basis: "auto" },
    { component: menu, basis: "auto", maxSize: actions.length * 2 },
    { component: activityText, basis: "auto" },
    { component: logScroll, basis: 0, grow: 1, minSize: 4 },
    { component: footer, basis: "auto" },
  ],
  { gap: 1 },
);
tui.setLayoutRoot(layout);
tui.setFocus(menu);

let busy = false;
let activeChild = null;
let logChild = null;
let logLines = [];
const services = createServices({ projectDir: PROJECT_DIR, run, log: appendLog });

function requestRender() {
  if (plainCheck) return;
  statusText.invalidate();
  activityText.invalidate();
  logText.invalidate();
  tui.requestRender();
}

function appendLog(text, kind = "normal") {
  const raw = stripTerminalSequences(String(text)).replace(/\r/g, "");
  sessionLog.append(raw.endsWith("\n") ? raw : `${raw}\n`);
  if (plainCheck) {
    const stream = kind === "error" ? process.stderr : process.stdout;
    stream.write(raw.endsWith("\n") ? raw : `${raw}\n`);
    return;
  }
  const cleaned = raw.trimEnd();
  if (!cleaned) return;
  const prefix = kind === "error" ? ansi.error("× ") : kind === "command" ? ansi.accent("› ") : "  ";
  logLines.push(...cleaned.split("\n").map((line) => `${prefix}${line}`));
  logLines = logLines.slice(-500);
  logText.setText(logLines.join("\n"));
  requestRender();
}

function setActivity(message, kind = "normal") {
  const color = kind === "error" ? ansi.error : kind === "success" ? ansi.success : ansi.warning;
  activityText.setText(color(message));
  requestRender();
}

async function exists(target, executable = false) {
  try {
    await access(target, executable ? constants.X_OK : constants.F_OK);
    return true;
  } catch {
    return false;
  }
}

function run(command, args, options = {}) {
  const { quiet = false, allowFailure = false, env = process.env } = options;
  if (!quiet) appendLog(`${command} ${args.join(" ")}`, "command");
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      cwd: PROJECT_DIR,
      env,
      stdio: ["ignore", "pipe", "pipe"],
    });
    activeChild = child;
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => {
      const value = chunk.toString();
      stdout += value;
      if (!quiet) appendLog(value);
    });
    child.stderr.on("data", (chunk) => {
      const value = chunk.toString();
      stderr += value;
      if (!quiet) appendLog(value, "error");
    });
    child.on("error", (error) => {
      if (activeChild === child) activeChild = null;
      if (allowFailure) resolve({ code: 127, stdout, stderr: `${stderr}${error.message}` });
      else reject(error);
    });
    child.on("close", (code) => {
      if (activeChild === child) activeChild = null;
      const result = { code: code ?? 1, stdout, stderr };
      if (result.code === 0 || allowFailure) resolve(result);
      else reject(new Error(`${command} exited with status ${result.code}`));
    });
  });
}

function statusLine(ok, label, detail) {
  const marker = ok ? ansi.success("●") : ansi.error("●");
  return `${marker} ${label.padEnd(12)} ${detail}`;
}

async function refreshStatus() {
  try {
    const state = await services.status();
    const workerDetail = !state.dockerReady
      ? "unavailable · start Docker"
      : !state.runtimeInstalled || !state.imageReady
        ? "unavailable · install the isolated runtime"
        : state.unsafeWorkers.length
          ? "unexpected isolation settings; stop workers and reconnect Pi"
      : state.workers.length
        ? `${state.workers.length} connected · ${state.workers.map((worker) => worker.Name.replace(/^\//, "")).join(", ")}`
        : "idle · Pi starts its worker when it connects";
    statusText.setText([
      statusLine(state.dockerReady, "Docker",
        state.dockerReady ? `running (${state.dockerVersion})` : "not running"),
      statusLine(state.runtimeInstalled && state.imageReady, "Runtime",
        !state.runtimeInstalled ? "not installed; run Install / update"
          : !state.dockerReady ? "installed; start Docker to verify"
          : !state.imageReady ? "image missing; run Install / update"
          : "installed · includes Chromium and search"),
      statusLine(state.ready, "Pi workers", workerDetail),
      ...(state.outdatedWorkers.length
        ? [ansi.warning("  Reconnect Pi to use the updated runtime.")] : []),
      ...(state.legacyRunning
        ? [ansi.warning("  Old standalone search is running; Start services will stop it.")] : []),
    ].join("\n"));
  } catch (error) {
    statusText.setText(ansi.error(error instanceof Error ? error.message : String(error)));
  }
  requestRender();
}

async function runEvaluation() {
  if (!(await exists(EVALUATOR, true))) {
    throw new Error("The evaluator is not installed yet. Run Install / update first.");
  }
  await run(EVALUATOR, ["check"]);
}

async function startLogs() {
  if (logChild) return;
  const auditLog = await services.prepareLogs();
  appendLog("Following the network broker’s domain-only connection records.");
  const child = spawn("/usr/bin/tail", ["-n", "100", "-F", auditLog],
    { cwd: PROJECT_DIR, stdio: ["ignore", "pipe", "pipe"] });
  logChild = child;
  child.stdout.on("data", (chunk) => appendLog(chunk.toString()));
  child.stderr.on("data", (chunk) => appendLog(chunk.toString(), "error"));
  child.on("error", (error) => {
    appendLog(error.message, "error");
    if (logChild === child) logChild = null;
    setActivity("Cannot follow network logs", "error");
  });
  child.on("close", () => {
    if (logChild === child) {
      logChild = null;
      setActivity("Live logs stopped", "normal");
    }
  });
  setActivity("Following live network records", "success");
}

function stopLogs() {
  const child = logChild;
  logChild = null;
  child?.kill("SIGTERM");
  setActivity("Live logs stopped", "normal");
}

async function performAction(value) {
  if (busy) {
    setActivity("Another operation is still running", "error");
    return;
  }
  if (value === "quit") {
    shutdown(0);
    return;
  }
  busy = true;
  setActivity(`Running: ${actions.find((item) => item.value === value)?.label || value}`);
  try {
    if (value === "rebuild") {
      await services.rebuild();
    } else if (value === "setup") {
      await services.install();
    } else if (value === "start") {
      await services.start();
    } else if (value === "stop") {
      await services.stop();
    } else if (value === "doctor") {
      await services.doctor();
    } else if (value === "logs") {
      if (logChild) stopLogs();
      else await startLogs();
    } else if (value === "evaluation") {
      await runEvaluation();
    } else if (value === "refresh") {
      await refreshStatus();
    }
    setActivity(value === "rebuild"
      ? "Rebuild and checks passed. Restart Pi to use the updated runtime."
      : "Operation complete", "success");
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    appendLog(message, "error");
    appendLog(`Full output saved: ${sessionLog.path}`);
    setActivity(message, "error");
  } finally {
    busy = false;
    await refreshStatus();
  }
}

function shutdown(code) {
  activeChild?.kill("SIGTERM");
  logChild?.kill("SIGTERM");
  tui.stop();
  process.exit(code);
}

menu.onSelect = (item) => void performAction(item.value);
menu.onCancel = () => shutdown(0);
tui.addInputListener((data) => {
  if (matchesKey(data, Key.ctrl("y"))) {
    void readFile(sessionLog.path, "utf8").then((text) => copyText(text)).then((ok) => {
      setActivity(ok ? "Full log copied" : `Copy failed. Log: ${sessionLog.path}`,
        ok ? "success" : "error");
    }).catch((error) => setActivity(error.message, "error"));
    return { consume: true };
  }
  if (matchesKey(data, Key.ctrl("o"))) {
    const child = spawn("/usr/bin/open", ["-t", sessionLog.path], { stdio: "ignore" });
    child.on("error", (error) => setActivity(error.message, "error"));
    child.on("close", (code) => {
      if (code !== 0) setActivity(`Cannot open log: ${sessionLog.path}`, "error");
    });
    return { consume: true };
  }
  if (matchesKey(data, Key.ctrl("c")) || data === "q") {
    shutdown(0);
    return { consume: true };
  }
  return undefined;
});

process.on("SIGTERM", () => shutdown(0));
process.on("uncaughtException", (error) => {
  try {
    tui.stop();
  } finally {
    console.error(error);
    process.exit(1);
  }
});

if (plainCheck) {
  try {
    await services.doctor();
  } catch (error) {
    appendLog(error instanceof Error ? error.message : String(error), "error");
    process.exitCode = 1;
  } finally {
    appendLog(`Full output saved: ${sessionLog.path}`);
  }
} else {
  tui.start();
  appendLog(`Session log: ${sessionLog.path}`);
  void refreshStatus();
}
