// Evaluation-only adapter: the configured Pi caller sees the actual MCP tool schemas.
// No filesystem, shell, or other model-visible tools are registered.
import { spawn, spawnSync } from "node:child_process";
import { writeFileSync } from "node:fs";
import { join } from "node:path";

const python = process.env.WEB_SEARCH_EVAL_PYTHON;
const root = process.env.WEB_SEARCH_EVAL_ROOT;
const policy = process.env.WEB_SEARCH_EVAL_POLICY || "current";
const description = spawnSync(python, ["-m", "web_research.research_benchmark", "describe", "--policy", policy], {
  cwd: root, encoding: "utf8", timeout: 30000,
});
if (description.status !== 0) throw new Error(description.stderr || "Cannot load MCP schemas");
const protocol = JSON.parse(description.stdout);
writeFileSync(join(process.env.WEB_SEARCH_EVAL_DIR, "protocol.json"), JSON.stringify(protocol, null, 2));

export default function (pi) {
  // Each bridge subprocess reopens SQLite. Serialize fixture calls to avoid initialization races;
  // the production MCP server instead shares its runtime within one process.
  let pending = Promise.resolve();
  pi.on("before_agent_start", async (event) => ({
    systemPrompt: event.systemPrompt + "\n\n" + protocol.instructions,
  }));
  for (const tool of protocol.tools) {
    pi.registerTool({
      ...tool, label: tool.name,
      async execute(_id, arguments_, signal) {
        const invocation = pending.then(() => new Promise((resolve, reject) => {
          const child = spawn(python, ["-m", "web_research.research_benchmark", "call",
            "--case", process.env.WEB_SEARCH_EVAL_CASE,
            "--directory", process.env.WEB_SEARCH_EVAL_DIR,
            "--policy", policy, "--max-calls", process.env.WEB_SEARCH_EVAL_MAX_CALLS || "20"],
          { cwd: root, stdio: ["pipe", "pipe", "pipe"] });
          let output = "";
          let error = "";
          const cancel = () => child.kill("SIGTERM");
          signal?.addEventListener("abort", cancel, { once: true });
          if (signal?.aborted) cancel();
          child.stdout.on("data", (data) => { output += data; });
          child.stderr.on("data", (data) => { error += data; });
          child.on("error", reject);
          child.on("close", (code) => {
            signal?.removeEventListener("abort", cancel);
            if (code !== 0) { reject(new Error(error.slice(-2000) || "Fixture call failed")); return; }
            try {
              const record = JSON.parse(output);
              if (record.is_error) { reject(new Error(record.error)); return; }
              resolve({ content: [{ type: "text", text: JSON.stringify(record.output) }], details: {} });
            } catch (error_) { reject(error_); }
          });
          child.stdin.end(JSON.stringify({ tool: tool.name, arguments: arguments_ }));
        }));
        pending = invocation.catch(() => {});
        return await invocation;
      },
    });
  }
}
