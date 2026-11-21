import { spawn } from "node:child_process";

// OSC 52 is not acknowledged by the terminal. Use the native pasteboard helper so
// the TUI reports success only after macOS accepts the write.
export function copyText(text, { platform = process.platform, spawnProcess = spawn } = {}) {
  if (platform !== "darwin") return Promise.resolve(false);
  return new Promise((resolve) => {
    let child;
    let timer;
    let finished = false;
    const finish = (ok) => {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      resolve(ok);
    };
    try {
      child = spawnProcess("/usr/bin/pbcopy", [], {
        stdio: ["pipe", "ignore", "ignore"],
        env: { ...process.env, LC_ALL: "en_US.UTF-8" },
      });
      child.on("error", () => finish(false));
      child.on("close", (code) => finish(code === 0));
      child.stdin.on("error", () => finish(false));
      timer = setTimeout(() => {
        child.kill("SIGTERM");
        finish(false);
      }, 3000);
      child.stdin.end(text, "utf8");
    } catch {
      finish(false);
    }
  });
}
