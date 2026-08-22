# Local Agentic Web Search

A local-first MCP research service for Pi. It exposes one tool, `web_search`, which searches through
SearXNG, reads promising pages, tracks evidence requirements, stops adaptively, and returns a cited
synthesis produced by a local model through an OpenAI-compatible endpoint.

This repository currently implements the first vertical slice from [ARCHITECTURE.md](ARCHITECTURE.md):

- SearXNG JSON discovery with caching and deduplication.
- Safe, bounded HTTP fetching with redirect revalidation.
- Trafilatura extraction with a basic HTML fallback.
- Automatic Crawl4AI/Chromium escalation for failed, short, or JavaScript-shell pages.
- Model-generated research requirements and gap-specific queries.
- Evidence ledger with source-count and primary-source gates.
- Expected-information-gain candidate ranking.
- Adaptive stopping with hard safety ceilings.
- Citation-ID validation and deterministic source lists.
- SQLite traces/cache, MCP progress, and cooperative cancellation.

Interactive Playwright actions, PDF/Docling, OCR, and MCP Tasks remain later milestones. Unsupported
or blocked pages are reported rather than silently treated as evidence.

## Terminal console

Run the standalone Pi-styled operator console from the project directory:

```bash
./web-search
```

The first launch installs the console's small Node.js dependency set. Inside the console you can:

- Install or update the Python application and Chromium runtime.
- Launch Docker Desktop when necessary and start, stop, or restart SearXNG.
- See Docker, SearXNG, model API, Chromium, and MCP readiness at a glance.
- Run the full readiness doctor and follow SearXNG logs.

The console does not replace or modify Pi. Pi continues to launch the single MCP server over stdio
when it needs `web_search`; the console is only an operator interface for installation and local
service management.

## Requirements

- Python 3.11 or newer.
- Node.js 22.19 or newer for the optional terminal console.
- Docker (recommended for SearXNG), or another SearXNG instance with JSON enabled.
- A running OpenAI-compatible chat-completions endpoint.
- A Pi MCP adapter/extension.

## 1. Install

```bash
cd /Users/donais/Documents/Projects/web-search
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev,browser]'
CRAWL4_AI_BASE_DIRECTORY="$PWD/.web-search-data" \
PLAYWRIGHT_BROWSERS_PATH="$PWD/.web-search-data/ms-playwright" \
  .venv/bin/crawl4ai-setup
```

Keep the two runtime paths together with the project. The MCP server and example Pi configuration
use the same location, so Pi does not depend on whichever working directory it was launched from.

## 2. Start SearXNG

```bash
docker compose -f docker/searxng/compose.yaml up -d
```

This binds SearXNG only to `127.0.0.1:8080` and enables JSON output. The configured upstream engines
still receive search queries; self-hosting the intermediary is not the same as making upstream
searches anonymous.

## 3. Configure the research model

Copy `config.example.env` to `.env`, set the exact model ID exposed by your model server, then export
the values in the shell that starts the MCP server:

```bash
cp config.example.env .env
set -a
source .env
set +a
```

Important settings:

| Variable | Default | Purpose |
|---|---|---|
| `WEB_SEARCH_SEARXNG_URL` | `http://127.0.0.1:8080` | SearXNG base URL |
| `WEB_SEARCH_MODEL_BASE_URL` | `http://127.0.0.1:8000/v1` | OpenAI-compatible API base |
| `WEB_SEARCH_MODEL_ID` | none | Required model ID |
| `WEB_SEARCH_DATA_DIR` | `.web-search-data` | SQLite cache and traces |
| `WEB_SEARCH_ALLOW_PRIVATE_URLS` | `false` | Development-only reader override |
| `WEB_SEARCH_ENABLE_CRAWL4AI` | `true` | Escalate incomplete pages to Chromium |

Do not enable private URL fetching for normal use. SearXNG and the model server have their own explicitly
configured local endpoints; public result pages remain protected against SSRF.

## 4. Check the services

```bash
.venv/bin/web-search-doctor
```

The command verifies the SearXNG JSON API, Crawl4AI package and Chromium runtime, the configured
model ID, the model server's `/models` endpoint, and the data directory.

## 5. Connect Pi

Point your Pi MCP extension/adapter at the absolute executable:

```text
/Users/donais/Documents/Projects/web-search/.venv/bin/web-search-mcp
```

Start from [integrations/pi/mcp-server.example.json](integrations/pi/mcp-server.example.json). Pi MCP
adapters differ in their outer configuration format, but the command and environment are the same.

The tool signature is:

```text
web_search(query, effort="auto", freshness=null)
```

- `quick`: a short lookup ceiling.
- `auto`: normal research and comparisons.
- `thorough`: a wider evidence search with a larger safety ceiling.

These modes set maximum time/search/page budgets; the controller stops earlier when evidence gates
pass and additional pages have low expected value.

## Development

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

The test suite is offline. It exercises the controller, evidence rules, citations, reader byte bounds,
URL safety, SearXNG and model-server protocol adapters, Crawl4AI failure handling, and the MCP tool
schema without requiring live SearXNG or model services.

## Security defaults

- Only HTTP(S) result URLs are accepted.
- Loopback, private, link-local, multicast, and metadata destinations are blocked.
- DNS and redirect destinations are checked on every hop.
- Crawl4AI installs a browser route guard that also blocks private subresources and redirects.
- Credentials in URLs and oversized responses are rejected.
- Browser sessions, authentication, form submission, shell access, and arbitrary JavaScript are not
  available to the research model.
- Retrieved pages are delimited as untrusted evidence and cannot change the research policy.
