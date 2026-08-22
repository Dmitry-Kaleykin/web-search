# Pi integration

The server exposes exactly one stdio MCP tool named `web_search`.

Pi currently connects to MCP servers through an extension/adapter. Configure that adapter to launch
the absolute `web-search-mcp` executable inside this project's virtual environment. The generic
server definition is in `mcp-server.example.json`; adapt only its outer configuration shape if your
chosen Pi MCP extension uses a different key or filename.

Required behavior from the Pi adapter:

- Import the server's `web_search` tool without renaming it.
- Advertise MCP sampling and run sampling requests through Pi's current model.
- Forward cancellation when Pi aborts the tool call.
- Forward MCP progress messages to Pi's tool-update UI.
- Keep the process environment shown in the example configuration.

The included `pi-mcp-adapter` configuration enables `sampling` and `samplingAutoApprove`. Automatic
approval avoids two confirmation dialogs for every internal model call during research. Scope that
setting only to MCP servers you trust. If automatic approval is disabled, interactive Pi sessions
can approve each request and response instead.

No model ID belongs in the MCP server entry. The adapter tries any explicit MCP model hint first,
then Pi's active model, then another available Pi model. This server sends no hint, so the active Pi
model is selected. `WEB_SEARCH_MODEL_ID` remains available only as a direct fallback when the client
does not advertise sampling.

The MCP server returns structured output containing `answer_markdown`, `sources`, `coverage`,
`stop_reason`, `stats`, and `warnings`. Pi should use `answer_markdown` as the researched answer and
retain the other fields for transparency.
