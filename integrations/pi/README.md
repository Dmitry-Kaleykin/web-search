# Pi integration

The server exposes exactly one stdio MCP tool named `web_search`.

Pi currently connects to MCP servers through an extension/adapter. Configure that adapter to launch
the absolute `web-search-mcp` executable inside this project's virtual environment. The generic
server definition is in `mcp-server.example.json`; adapt only its outer configuration shape if your
chosen Pi MCP extension uses a different key or filename.

Required behavior from the Pi adapter:

- Import the server's `web_search` tool without renaming it.
- Forward cancellation when Pi aborts the tool call.
- Forward MCP progress messages to Pi's tool-update UI.
- Keep the process environment shown in the example configuration.

The MCP server returns structured output containing `answer_markdown`, `sources`, `coverage`,
`stop_reason`, `stats`, and `warnings`. Pi should use `answer_markdown` as the researched answer and
retain the other fields for transparency.

