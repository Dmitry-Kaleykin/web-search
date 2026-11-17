import { access, mkdir, open, readFile } from "node:fs/promises";
import { constants } from "node:fs";
import path from "node:path";

const LEGACY_CONTAINER = "local-web-search-searxng";
const WORKER_ENTRYPOINT = ["python", "-m", "web_research.isolation.worker"];

async function exists(target, executable = false) {
  try {
    await access(target, executable ? constants.X_OK : constants.F_OK);
    return true;
  } catch {
    return false;
  }
}

export function createServices({ projectDir, run, log = () => {}, platform = process.platform,
  wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms)) }) {
  const python = path.join(projectDir, ".venv/bin/python");
  const manifest = path.join(projectDir, "build/isolation.json");
  const broker = path.join(projectDir, "build/Web Search Network.app/Contents/MacOS/WebSearchNetwork");
  const launcher = path.join(projectDir, "web-search-isolated");
  const auditLog = path.join(projectDir, ".web-search-data/isolation/network.jsonl");
  // Docker operations use the local endpoint pinned by the installed runtime. Do not
  // inherit remote contexts, proxy settings or credentials from the terminal's environment.
  const localEnv = { PATH: process.env.PATH, HOME: process.env.HOME };

  async function runtime() {
    let config;
    try {
      config = JSON.parse(await readFile(manifest, "utf8"));
    } catch (error) {
      if (error.code === "ENOENT") return null;
      throw new Error("The installed runtime configuration is unreadable. Run Install / update.");
    }
    if (!config || !/^sha256:[a-f0-9]{64}$/.test(config.image) ||
        typeof config.docker !== "string" || !path.isAbsolute(config.docker) ||
        typeof config.docker_endpoint !== "string" || !config.docker_endpoint.startsWith("unix:///") ||
        typeof config.volume !== "string" || !/^[a-zA-Z0-9][a-zA-Z0-9_.-]+$/.test(config.volume)) {
      throw new Error("The installed runtime configuration is invalid. Run Install / update.");
    }
    return config;
  }

  async function docker(args, options = {}, config = undefined) {
    config = config === undefined ? await runtime() : config;
    const prefix = config ? ["--host", config.docker_endpoint] : [];
    return run(config?.docker || "docker", [...prefix, ...args], { ...options, env: localEnv });
  }

  async function workerContainers(config) {
    const listed = await docker(["ps", "--filter", `volume=${config.volume}`, "--format", "{{.ID}}"],
      { quiet: true }, config);
    const ids = listed.stdout.trim().split(/\s+/).filter((id) => /^[a-f0-9]{12,64}$/.test(id));
    if (!ids.length) return [];
    const details = await docker(["inspect", ...ids], { quiet: true }, config);
    return JSON.parse(details.stdout).filter((container) =>
      container.State?.Running &&
      JSON.stringify(container.Config?.Entrypoint) === JSON.stringify(WORKER_ENTRYPOINT) &&
      container.Config?.Env?.includes("WEB_SEARCH_ISOLATED_WORKER=1") &&
      container.Mounts?.some((mount) => mount.Type === "volume" &&
        mount.Name === config.volume && mount.Destination === "/data"),
    );
  }

  async function legacyRunning(config) {
    const legacy = await docker(["inspect", LEGACY_CONTAINER, "--format", "{{.State.Running}}"],
      { quiet: true, allowFailure: true }, config);
    return legacy.code === 0 && legacy.stdout.trim() === "true";
  }

  async function status() {
    const config = await runtime();
    const engine = await docker(["info", "--format", "{{.ServerVersion}}"],
      { quiet: true, allowFailure: true }, config);
    const filesReady = await exists(python, true) && await exists(launcher, true) &&
      await exists(broker, true);
    let imageReady = false;
    let workers = [];
    let legacy = false;
    if (engine.code === 0) {
      if (config) {
        const image = await docker(["image", "inspect", config.image, "--format", "{{.Id}}"],
          { quiet: true, allowFailure: true }, config);
        imageReady = image.code === 0 && image.stdout.trim() === config.image;
        workers = await workerContainers(config);
      }
      legacy = await legacyRunning(config);
    }
    const unsafeWorkers = workers.filter((worker) =>
      worker.HostConfig?.NetworkMode !== "none" || !worker.HostConfig?.ReadonlyRootfs);
    return {
      dockerReady: engine.code === 0,
      dockerVersion: engine.stdout.trim(),
      runtimeInstalled: Boolean(config && filesReady),
      imageReady,
      ready: Boolean(engine.code === 0 && config && filesReady && imageReady && !unsafeWorkers.length),
      workers,
      unsafeWorkers,
      outdatedWorkers: workers.filter((worker) => worker.Image !== config?.image),
      legacyRunning: legacy,
    };
  }

  async function ensureDocker() {
    // Installation can repair a damaged manifest; ordinary Start/status still fail closed.
    const config = await runtime().catch(() => null);
    let result = await docker(["info", "--format", "{{.ServerVersion}}"],
      { quiet: true, allowFailure: true }, config);
    if (result.code === 0) return;
    if (platform !== "darwin" || !(await exists("/Applications/Docker.app"))) {
      throw new Error("Docker is not running. Install or start Docker before continuing.");
    }
    log("Launching Docker Desktop…", "command");
    await run("open", ["-a", "Docker"]);
    for (let attempt = 0; attempt < 60; attempt += 1) {
      await wait(1000);
      result = await docker(["info", "--format", "{{.ServerVersion}}"],
        { quiet: true, allowFailure: true }, config);
      if (result.code === 0) return;
    }
    throw new Error("Docker Desktop did not become ready within 60 seconds.");
  }

  async function stopLegacy(config) {
    if (await legacyRunning(config)) {
      log("Stopping the old standalone search service; isolated search includes its own.");
      await docker(["stop", LEGACY_CONTAINER], {}, config);
    }
  }

  async function start() {
    await ensureDocker();
    const state = await status();
    if (!state.runtimeInstalled || !state.imageReady) {
      throw new Error("The isolated runtime is not installed. Run Install / update and start.");
    }
    if (state.unsafeWorkers.length) {
      throw new Error("A search worker has unexpected isolation settings. Stop workers and reconnect Pi.");
    }
    await stopLegacy(await runtime());
    log(state.workers.length
      ? "Isolated search is ready. Pi already has a connected search worker."
      : "Isolated search is ready. Pi starts its worker when it connects; no standalone service is needed.");
    if (state.outdatedWorkers.length) log("Reconnect Pi to use the updated runtime.");
  }

  async function stop() {
    const config = await runtime();
    if (config) {
      const workers = await workerContainers(config);
      if (workers.length) await docker(["stop", "--time", "10", ...workers.map((w) => w.Id)], {}, config);
      log(workers.length ? "Search workers stopped. Reconnect Pi before searching again."
        : "No Pi search workers are running.");
    }
    await stopLegacy(config);
  }

  async function install() {
    await ensureDocker();
    if (!(await exists(python, true))) {
      await run("python3", ["-m", "venv", path.join(projectDir, ".venv")]);
    }
    await run(python, ["-m", "pip", "install", "-e", "."]);
    await run(python, [path.join(projectDir, "tools/build_isolated.py")], { env: localEnv });
    log("Runtime built. Reconnect Pi to use it; Little Snitch may ask to verify the rebuilt executable.");
    await start();
  }

  async function doctor() {
    await start();
    await run(python, [path.join(projectDir, "tools/check_isolation.py")], { env: localEnv });
  }

  async function prepareLogs() {
    await mkdir(path.dirname(auditLog), { recursive: true, mode: 0o700 });
    const file = await open(auditLog, "a", 0o600);
    await file.close();
    return auditLog;
  }

  return { status, start, stop, install, doctor, prepareLogs };
}
