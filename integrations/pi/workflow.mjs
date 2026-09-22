import { readFileSync } from "node:fs";

const marker = "[Local Web Search research workflow]";
const workflow = readFileSync(new URL("../../src/web_research/research_workflow.md", import.meta.url), "utf8");

export default function (pi) {
  pi.on("before_agent_start", async (event) => {
    const active = new Set(pi.getActiveTools());
    const tools = pi.getAllTools().filter((tool) => active.has(tool.name));
    const search = tools.some((tool) => tool.description?.startsWith(
      "Search the web and return ranked URLs, titles, snippets,"));
    const read = tools.some((tool) => tool.description?.startsWith(
      "Fetch and extract content from an already-known public HTTP(S) URL."));
    if (!search || !read || event.systemPrompt.includes(marker)) return;
    // Reapplied each turn, including after compaction. The caller retains its evidence note
    // in conversation summaries; this extension stores no research state or private source text.
    return { systemPrompt: `${event.systemPrompt}\n\n${marker}\n${workflow}` };
  });
}
