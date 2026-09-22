import assert from "node:assert/strict";
import test from "node:test";
import extension from "./workflow.mjs";

const tools = [
  { name: "prefix_web_search", description: "Search the web and return ranked URLs, titles, snippets, and reported publication dates." },
  { name: "prefix_read_url", description: "Fetch and extract content from an already-known public HTTP(S) URL." },
];

function handler(active) {
  let callback;
  extension({
    getActiveTools: () => active,
    getAllTools: () => tools,
    on: (event, fn) => { assert.equal(event, "before_agent_start"); callback = fn; },
  });
  return callback;
}

test("only augments turns with both of this server's tools enabled", async () => {
  const original = "The user's existing system prompt.";
  const inactive = await handler([tools[0].name])({ systemPrompt: original });
  assert.equal(inactive, undefined);
  const result = await handler(tools.map((tool) => tool.name))({ systemPrompt: original });
  assert.ok(result.systemPrompt.startsWith(original));
  assert.ok(result.systemPrompt.includes("blocked:"));
  const again = await handler(tools.map((tool) => tool.name))(result);
  assert.equal(again, undefined);
});
