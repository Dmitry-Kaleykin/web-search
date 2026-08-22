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

const UI_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const PROJECT_DIR = path.resolve(UI_DIR, "..");
const COMPOSE_FILE = path.join(PROJECT_DIR, "docker", "searxng", "compose.yaml");
const DATA_DIR = path.join(PROJECT_DIR, ".web-search-data");
const BROWSER_DIR = path.join(DATA_DIR, "ms-playwright");
const PYTHON = path.join(PROJECT_DIR, ".venv", "bin", "python");
const PIP = path.join(PROJECT_DIR, ".venv", "bin", "python");
const CRAWL4AI_SETUP = path.join(PROJECT_DIR, ".venv", "bin", "crawl4ai-setup");
const MCP_SERVER = path.join(PROJECT_DIR, ".venv", "bin", "web-search-mcp");
const DOCTOR = path.join(PROJECT_DIR, ".venv", "bin", "web-search-doctor");

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
    value: "setup",
    label: "Install / update and start",
    description: "Set up Python, Chromium, Docker, and SearXNG",
  },
  {
    value: "start",
    label: "Start services",
    description: "Launch Docker Desktop if needed and start SearXNG",
  },
  {
    value: "stop",
    label: "Stop services",
    description: "Stop the project SearXNG container",
  },
  {
    value: "restart",
    label: "Restart services",
    description: "Restart SearXNG without reinstalling anything",
  },
  {
    value: "doctor",
    label: "Run readiness checks",
    description: "Check browser, search API, model strategy, and configuration",
  },
  {
    value: "logs",
    label: "Toggle live logs",
    description: "Follow or stop following SearXNG container output",
  },
  {
    value: "refresh",
    label: "Refresh status",
    description: "Recheck every local component and endpoint",
  },
  { value: "quit", label: "Quit", description: "Leave services in their current state" },
];

const terminal = new ProcessTerminal();
const tui = new TuiAltScreen(terminal, false, undefined, {
  mouse: true,
  searchMatchStyle: (text) => `\x1b[30;43m${text}\x1b[0m`,
  searchCurrentMatchStyle: (text) => `\x1b[30;46m${text}\x1b[0m`,
});

const header = new Text(
  `${ansi.accent(ansi.bold("Local Agentic Web Search"))}\n${ansi.dim(
    "Operator console · MCP remains the Pi integration boundary",
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
  ansi.dim("↑↓ choose · enter run · esc/q quit · ctrl+shift+f search logs"),
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

function requestRender() {
  statusText.invalidate();
  activityText.invalidate();
  logText.invalidate();
  tui.requestRender();
}

function appendLog(text, kind = "normal") {
  const cleaned = stripTerminalSequences(String(text)).replace(/\r/g, "").trimEnd();
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

async function readEnvironment() {
  const values = {};
  try {
    const contents = await readFile(path.join(PROJECT_DIR, ".env"), "utf8");
    for (const rawLine of contents.split("\n")) {
      const line = rawLine.trim();
      if (!line || line.startsWith("#")) continue;
      const separator = line.indexOf("=");
      if (separator < 1) continue;
      const key = line.slice(0, separator).trim();
      let value = line.slice(separator + 1).trim();
      if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
        value = value.slice(1, -1);
      }
      values[key] = value;
    }
  } catch {
    // An absent .env is represented in the status output.
  }
  return { ...values, ...process.env };
}

async function fetchOk(url, timeoutMs = 3500, headers = {}) {
  try {
    const response = await fetch(url, { headers, signal: AbortSignal.timeout(timeoutMs) });
    return { ok: response.ok, status: response.status, json: await response.json().catch(() => null) };
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : String(error) };
  }
}

function statusLine(ok, label, detail) {
  const marker = ok ? ansi.success("●") : ansi.error("●");
  return `${marker} ${label.padEnd(12)} ${detail}`;
}

async function refreshStatus() {
  const environment = await readEnvironment();
  const docker = await run("docker", ["info", "--format", "{{.ServerVersion}}"], {
    quiet: true,
    allowFailure: true,
  });
  const compose = docker.code === 0
    ? await run(
        "docker",
        ["compose", "-f", COMPOSE_FILE, "ps", "--status", "running", "--services"],
        { quiet: true, allowFailure: true },
      )
    : { code: 1, stdout: "" };
  const searchUrl = environment.WEB_SEARCH_SEARXNG_URL || "http://127.0.0.1:8080";
  const search = await fetchOk(`${searchUrl.replace(/\/$/, "")}/search?q=health&format=json`);
  const fallbackModelId = environment.WEB_SEARCH_MODEL_ID || "";
  let model = { ok: true };
  if (fallbackModelId) {
    const modelBase = environment.WEB_SEARCH_MODEL_BASE_URL || "http://127.0.0.1:8000/v1";
    const modelHeaders = environment.WEB_SEARCH_MODEL_API_KEY
      ? { Authorization: `Bearer ${environment.WEB_SEARCH_MODEL_API_KEY}` }
      : {};
    model = await fetchOk(`${modelBase.replace(/\/$/, "")}/models`, 3500, modelHeaders);
  }
  const pythonReady = await exists(PYTHON, true);
  const mcpReady = await exists(MCP_SERVER, true);
  const browserReady = await exists(BROWSER_DIR);
  const searxngRunning = compose.code === 0 && compose.stdout.split(/\s+/).includes("searxng");

  statusText.setText(
    [
      statusLine(
        docker.code === 0,
        "Docker",
        docker.code === 0 ? `running (${docker.stdout.trim()})` : "not running",
      ),
      statusLine(
        searxngRunning,
        "SearXNG",
        search.ok
          ? "running; JSON API ready"
          : searxngRunning
            ? "container running; API unavailable"
            : "stopped",
      ),
      statusLine(
        model.ok,
        "Model",
        fallbackModelId
          ? model.ok
            ? `MCP sampling preferred; fallback ${fallbackModelId} reachable`
            : `MCP sampling preferred; fallback ${fallbackModelId} unavailable`
          : "dynamic through MCP client sampling",
      ),
      statusLine(browserReady, "Chromium", browserReady ? "runtime installed" : "runtime missing"),
      statusLine(
        pythonReady && mcpReady,
        "MCP",
        pythonReady && mcpReady ? "ready; launched on demand by Pi" : "not installed",
      ),
    ].join("\n"),
  );
  requestRender();
}

async function ensureDocker() {
  let result = await run("docker", ["info", "--format", "{{.ServerVersion}}"], {
    quiet: true,
    allowFailure: true,
  });
  if (result.code === 0) return;
  if (process.platform !== "darwin" || !(await exists("/Applications/Docker.app"))) {
    throw new Error("Docker is not running. Install or start Docker before continuing.");
  }
  appendLog("Launching Docker Desktop…", "command");
  await run("open", ["-a", "Docker"]);
  for (let attempt = 0; attempt < 60; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 1000));
    result = await run("docker", ["info", "--format", "{{.ServerVersion}}"], {
      quiet: true,
      allowFailure: true,
    });
    if (result.code === 0) return;
  }
  throw new Error("Docker Desktop did not become ready within 60 seconds.");
}

async function installApplication() {
  if (!(await exists(PYTHON, true))) {
    await run("python3", ["-m", "venv", path.join(PROJECT_DIR, ".venv")]);
  }
  await run(PIP, ["-m", "pip", "install", "-e", ".[browser]"]);
  const runtimeEnvironment = {
    ...process.env,
    CRAWL4_AI_BASE_DIRECTORY: DATA_DIR,
    PLAYWRIGHT_BROWSERS_PATH: BROWSER_DIR,
  };
  await run(CRAWL4AI_SETUP, [], { env: runtimeEnvironment });
}

async function startServices({ pull = false } = {}) {
  await ensureDocker();
  if (pull) {
    await run("docker", ["compose", "-f", COMPOSE_FILE, "pull"]);
  }
  await run("docker", ["compose", "-f", COMPOSE_FILE, "up", "-d"]);
}

async function stopServices() {
  if (logChild) stopLogs();
  await run("docker", ["compose", "-f", COMPOSE_FILE, "stop"]);
}

async function restartServices() {
  await ensureDocker();
  await run("docker", ["compose", "-f", COMPOSE_FILE, "restart"]);
}

async function runDoctor() {
  if (!(await exists(DOCTOR, true))) {
    throw new Error("The application is not installed yet. Run Install / update first.");
  }
  const environment = await readEnvironment();
  await run(DOCTOR, [], {
    allowFailure: true,
    env: {
      ...process.env,
      ...environment,
      CRAWL4_AI_BASE_DIRECTORY: DATA_DIR,
      PLAYWRIGHT_BROWSERS_PATH: BROWSER_DIR,
    },
  });
}

function startLogs() {
  if (logChild) return;
  appendLog("docker compose logs --tail 100 --follow", "command");
  logChild = spawn(
    "docker",
    ["compose", "-f", COMPOSE_FILE, "logs", "--tail", "100", "--follow", "--no-color"],
    { cwd: PROJECT_DIR, stdio: ["ignore", "pipe", "pipe"] },
  );
  logChild.stdout.on("data", (chunk) => appendLog(chunk.toString()));
  logChild.stderr.on("data", (chunk) => appendLog(chunk.toString(), "error"));
  logChild.on("close", () => {
    logChild = null;
    setActivity("Live logs stopped", "normal");
  });
  setActivity("Following live SearXNG logs", "success");
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
  if (value === "logs") {
    if (logChild) stopLogs();
    else startLogs();
    return;
  }

  busy = true;
  setActivity(`Running: ${actions.find((item) => item.value === value)?.label || value}`);
  try {
    if (value === "setup") {
      await installApplication();
      await startServices({ pull: true });
    } else if (value === "start") {
      await startServices();
    } else if (value === "stop") {
      await stopServices();
    } else if (value === "restart") {
      await restartServices();
    } else if (value === "doctor") {
      await runDoctor();
    } else if (value === "refresh") {
      await refreshStatus();
    }
    setActivity("Operation complete", "success");
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    appendLog(message, "error");
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

tui.start();
void refreshStatus();
