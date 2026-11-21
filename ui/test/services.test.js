import assert from "node:assert/strict";
import { mkdtemp, mkdir, writeFile, readFile, rm, stat, unlink } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { createServices } from "../src/services.js";

const IMAGE = `sha256:${"a".repeat(64)}`;
const VOLUME = "test-isolated-search-data";

function worker(overrides = {}) {
  return {
    Id: "b".repeat(64), Name: "/reverent_hofstadter", Image: IMAGE,
    State: { Running: true },
    Config: { Entrypoint: ["python", "-m", "web_research.isolation.worker"],
      Env: ["WEB_SEARCH_ISOLATED_WORKER=1"] },
    HostConfig: { NetworkMode: "none", ReadonlyRootfs: true },
    Mounts: [{ Type: "volume", Name: VOLUME, Destination: "/data" }],
    ...overrides,
  };
}

async function fixture(t, { workers = [], legacy = false, image = true, running = true,
  failScript = null } = {}) {
  const projectDir = await mkdtemp(path.join(os.tmpdir(), "ws-console-"));
  t.after(() => rm(projectDir, { recursive: true, force: true }));
  const config = { image: IMAGE, docker: "/usr/local/bin/docker",
    docker_endpoint: "unix:///tmp/local-docker.sock", volume: VOLUME };
  for (const file of [".venv/bin/python", "web-search-isolated",
    "build/Web Search Network.app/Contents/MacOS/WebSearchNetwork"]) {
    const target = path.join(projectDir, file);
    await mkdir(path.dirname(target), { recursive: true });
    await writeFile(target, "test executable", { mode: 0o755 });
  }
  await writeFile(path.join(projectDir, "build/isolation.json"), JSON.stringify(config));
  const calls = [], logs = [];
  const run = async (command, args, options = {}) => {
    calls.push({ command, args, options });
    const ok = (stdout = "") => ({ code: 0, stdout, stderr: "" });
    if (command.endsWith("python")) {
      if (failScript && args[0]?.endsWith(failScript)) throw new Error(`Simulated failure: ${failScript}`);
      return ok();
    }
    assert.equal(command, config.docker);
    assert.deepEqual(args.slice(0, 2), ["--host", config.docker_endpoint]);
    const op = args.slice(2);
    if (op[0] === "info") return running ? ok("30.0.0\n") : { code: 1, stdout: "" };
    if (op[0] === "image") return image ? ok(IMAGE + "\n") : { code: 1, stdout: "" };
    if (op[0] === "ps") return ok(workers.map((w) => w.Id.slice(0, 12)).join("\n"));
    if (op[0] === "inspect" && op[1] === "local-web-search-searxng") return ok(`${legacy}\n`);
    if (op[0] === "inspect") return ok(JSON.stringify(workers));
    if (op[0] === "stop") {
      if (op[1] === "local-web-search-searxng") legacy = false;
      return ok();
    }
    assert.fail(`Unexpected command: ${args.join(" ")}`);
  };
  const services = createServices({ projectDir, run, log: (message) => logs.push(message),
    platform: "test" });
  return { services, calls, logs, projectDir };
}

test("idle isolated runtime is ready without a standalone SearXNG endpoint", async (t) => {
  const { services, calls } = await fixture(t);
  const state = await services.status();
  assert.equal(state.ready, true);
  assert.equal(state.workers.length, 0);
  assert.equal(state.legacyRunning, false);
  assert.ok(!calls.some(({ args }) => args.includes("compose") || args.includes("run")));
});

test("Start services stops the legacy service and leaves Pi's worker connected", async (t) => {
  const existing = worker();
  const { services, calls, logs } = await fixture(t, { legacy: true, workers: [existing] });
  await services.start();
  const stops = calls.filter(({ args }) => args.includes("stop"));
  assert.equal(stops.length, 1);
  assert.equal(stops[0].args.at(-1), "local-web-search-searxng");
  assert.ok(logs.some((message) => message.includes("already has a connected")));
  assert.ok(!calls.some(({ args }) => args.includes("compose") || args.includes("build") ||
    args.includes("up") || args.includes("run")));
});

test("Start services does not launch an extra worker when Pi is disconnected", async (t) => {
  const { services, calls, logs } = await fixture(t);
  await services.start();
  assert.ok(logs.some((message) => message.includes("when it connects")));
  assert.ok(!calls.some(({ args }) => args.includes("run")));
});

test("missing image cannot be reported as ready or silently replaced", async (t) => {
  const { services, calls } = await fixture(t, { image: false });
  assert.equal((await services.status()).ready, false);
  await assert.rejects(services.start(), /not installed/);
  assert.ok(!calls.some(({ args }) => args.includes("pull") || args.includes("build")));
});

test("a missing broker executable prevents readiness", async (t) => {
  const { services, projectDir } = await fixture(t);
  await unlink(path.join(projectDir, "build/Web Search Network.app/Contents/MacOS/WebSearchNetwork"));
  assert.equal((await services.status()).ready, false);
});

test("Stop workers selects only the isolated MCP workers using this project's volume", async (t) => {
  const ours = worker();
  const unrelated = worker({ Id: "c".repeat(64), Config: { Entrypoint: ["other-app"] } });
  const otherVolume = worker({ Id: "d".repeat(64), Mounts: [] });
  const { services, calls, logs } = await fixture(t, { workers: [ours, unrelated, otherVolume] });
  await services.stop();
  const stops = calls.filter(({ args }) => args.includes("stop"));
  assert.equal(stops.length, 1);
  assert.deepEqual(stops[0].args.slice(2), ["stop", "--time", "10", ours.Id]);
  assert.ok(logs.some((message) => message.includes("Reconnect Pi")));
});

test("workers from an earlier build are still found and can be stopped", async (t) => {
  const { services } = await fixture(t, { workers: [worker({ Image: `sha256:${"e".repeat(64)}` })] });
  const state = await services.status();
  assert.equal(state.outdatedWorkers.length, 1);
  assert.equal(state.workers.length, 1);
});

test("unexpected worker isolation is reported and prevents Start from claiming readiness", async (t) => {
  const { services } = await fixture(t, {
    workers: [worker({ HostConfig: { NetworkMode: "bridge", ReadonlyRootfs: false } })],
  });
  assert.equal((await services.status()).ready, false);
  await assert.rejects(services.start(), /unexpected isolation/);
});

test("readiness checks use the complete isolated acceptance check", async (t) => {
  const { services, calls, projectDir } = await fixture(t);
  await services.doctor();
  const check = calls.find(({ args }) => args[0] === path.join(projectDir, "tools/check_isolation.py"));
  assert.ok(check);
  assert.ok(!calls.some(({ command }) => command.endsWith("web-search-doctor")));
});

test("installation builds the isolated runtime instead of host Chromium or Compose", async (t) => {
  const { services, calls, projectDir } = await fixture(t);
  await services.install();
  assert.ok(calls.some(({ args }) => args[0] === path.join(projectDir, "tools/build_isolated.py")));
  assert.ok(!calls.some(({ command, args }) => command.endsWith("crawl4ai-setup") || args.includes("compose")));
});

test("rebuild checks the new runtime without pip or disconnecting Pi's existing worker", async (t) => {
  const { services, calls, logs, projectDir } = await fixture(t, { workers: [worker()] });
  await services.rebuild();
  const pythonCalls = calls.filter(({ command }) => command.endsWith("python"));
  assert.deepEqual(pythonCalls.map(({ args }) => args), [
    [path.join(projectDir, "tools/build_isolated.py")],
    [path.join(projectDir, "tools/check_isolation.py")],
  ]);
  for (const { options } of pythonCalls) {
    assert.deepEqual(Object.keys(options.env).sort(), ["HOME", "PATH"]);
  }
  assert.ok(!calls.some(({ args }) => args.includes("pip") || args.includes("stop")));
  assert.ok(logs.findIndex((message) => message.includes("Docker may download")) <
    logs.findIndex((message) => message.includes("Runtime rebuilt")));
  assert.ok(logs.some((message) => message.includes("Little Snitch")));
  assert.match(logs.at(-1), /checks passed.*Restart Pi/);
});

for (const failScript of ["build_isolated.py", "check_isolation.py"]) {
  test(`rebuild does not report success after ${failScript} fails`, async (t) => {
    const { services, calls, logs } = await fixture(t, { failScript });
    await assert.rejects(services.rebuild(), /Simulated failure/);
    assert.ok(!logs.some((message) => message.includes("checks passed")));
    if (failScript === "build_isolated.py") {
      assert.ok(!calls.some(({ args }) => args[0]?.endsWith("check_isolation.py")));
    }
  });
}

test("rebuild requires the existing Python environment without installing one", async (t) => {
  const { services, calls, projectDir } = await fixture(t);
  await unlink(path.join(projectDir, ".venv/bin/python"));
  await assert.rejects(services.rebuild(), /Python environment is missing/);
  assert.equal(calls.length, 0);
});

test("live logs use the broker audit file and preserve existing records", async (t) => {
  const { services } = await fixture(t);
  const log = await services.prepareLogs();
  await writeFile(log, "test domain record\n");
  assert.equal(await services.prepareLogs(), log);
  assert.equal(await readFile(log, "utf8"), "test domain record\n");
  assert.equal((await stat(log)).mode & 0o777, 0o600);
});

test("remote Docker endpoints in a runtime manifest fail closed", async (t) => {
  const { services, projectDir } = await fixture(t);
  const file = path.join(projectDir, "build/isolation.json");
  const config = JSON.parse(await readFile(file, "utf8"));
  config.docker_endpoint = "tcp://remote.example:2375";
  await writeFile(file, JSON.stringify(config));
  await assert.rejects(services.status(), /invalid/);
});

test("Docker commands do not inherit terminal proxy or remote-context settings", async (t) => {
  const { services, calls } = await fixture(t);
  await services.status();
  for (const call of calls) {
    assert.deepEqual(Object.keys(call.options.env).sort(), ["HOME", "PATH"]);
  }
});
