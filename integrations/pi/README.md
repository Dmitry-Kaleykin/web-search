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

The included `pi-mcp-adapter` configuration enables sampling globally as a capability, but places
`samplingAutoApprove` on the `web-search` server entry. Automatic approval avoids two confirmation
dialogs for every internal model call without trusting other MCP servers. This per-server option is
provided by `scoped-mcp`'s version-pinned adapter patch. If automatic approval is disabled,
interactive Pi sessions can approve each request and response instead.

The example also gives this server a 30-minute outer `requestTimeoutMs`. That is not the research
target: it prevents the adapter's short default timeout from canceling a legitimate multi-stage
call. Web-search still enforces its smaller per-model, active-browsing, search, and page ceilings,
and canceling the Pi tool call aborts the current sampling request immediately.

No model ID belongs in the MCP server entry. The adapter tries any explicit MCP model hint first,
then Pi's active model, then another available Pi model. This server sends no hint, so the active Pi
model is selected. `WEB_SEARCH_MODEL_ID` remains available only as a direct fallback when the client
does not advertise sampling.

The server deliberately negotiates the 2025-11-25 handshake protocol over stdio. Its adaptive
controller performs model calls between searches and page reads, which needs the duplex sampling
back-channel. `pi-mcp-adapter` uses automatic negotiation and falls back to this protocol.

Users of TUN proxies with fake-IP DNS may need `WEB_SEARCH_ALLOW_PROXY_FAKE_IPS=true`. This permits
only synthetic `198.18.0.0/15` answers for hostname URLs; it does not disable the private-network
guard.

The MCP server returns structured output containing `answer_markdown`, `sources`, `coverage`,
`stop_reason`, `stats`, and `warnings`. Pi should use `answer_markdown` as the researched answer and
retain the other fields for transparency.

Effort-level time limits count only active search and page retrieval. Pi model inference and
approval time use the independent `WEB_SEARCH_MODEL_TIMEOUT_SECONDS` limit, so a slow planning call
cannot consume the entire browsing allowance before the first query runs.
