# Project instructions

## Network isolation is a user requirement

The user relies on Little Snitch to grant this project's runtime internet access only to
`build/Web Search Network.app/Contents/MacOS/WebSearchNetwork`. Do not require general
internet allowances for Terminal, host Python, host Chromium/chrome-headless-shell, or Docker
to perform runtime searches or page reads.

- Run public-network retrieval and rendering checks through `web-search-isolated` and the
  native broker. `tools/check_isolation.py` uses that route for its public MCP checks.
- Do not run host-side HTTPReader/Crawl4AIReader smoke scripts, the unrestricted doctor, or
  unrestricted live retrieval/research benchmarks against public websites unless the user
  explicitly authorizes host networking for that work.
- Host-side tests may use mocked/offline responses and temporary loopback fixture servers.
  Distinguish those local checks from public internet access when reporting validation.
- Rebuild the isolated image before claiming that public checks validate changed source code.
  The installed image is pinned and does not load the host checkout's current Python files.
- Builds can download dependencies through Docker. Explain this distinction before starting
  an installation/update; never request broad firewall permissions as a runtime workaround.
- Preserve the existing fail-closed launcher and broker restrictions. Do not loosen cookie,
  method, destination, file-access, or container-network policies to improve site coverage
  without an explicit user decision.

Pi's configured web-search profile lives at
`~/.pi/agent/extensions/scoped-mcp/scoped-mcp.json` and should launch the absolute project
`web-search-isolated` path. Inspect only the relevant profile fields; do not print credentials
or unrelated configuration.
