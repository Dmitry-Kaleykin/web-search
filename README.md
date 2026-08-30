# Local Agentic Web Search

A local-first MCP research service for Pi. It exposes `read_url` for extracting an already-known URL
and `web_search` for discovering sources, tracking evidence requirements, stopping adaptively, and
returning a cited synthesis. Through MCP sampling, research automatically uses the model active in
the calling Pi session; an OpenAI-compatible endpoint can be configured as a fallback for clients
without sampling support.

This repository currently implements the first vertical slice from [ARCHITECTURE.md](ARCHITECTURE.md):

- SearXNG JSON discovery with caching and deduplication.
- Safe, bounded HTTP fetching with redirect revalidation.
- Trafilatura extraction with a basic HTML fallback.
- Automatic Crawl4AI/Chromium escalation for failed retrievals, JavaScript shells, loading
  placeholders, browser-check interstitials, and responses with no extracted content.
- Model-generated research requirements and gap-specific queries.
- Evidence ledger with claim-support, source-count, and domain-diversity gates.
- Expected-information-gain candidate ranking.
- Adaptive stopping with hard safety ceilings.
- Citation-ID validation and deterministic source lists.
- SQLite traces/cache, MCP progress with compact evidence summaries, and cooperative cancellation.

Interactive Playwright actions, PDF/Docling, OCR, and MCP Tasks remain later milestones. Unsupported
or blocked pages are reported rather than silently treated as evidence.

## Terminal console

Run the standalone Pi-styled operator console from any directory:

```bash
web-search
```

On this machine, `~/.local/bin/web-search` points to the project launcher. The launcher resolves its
real location through the symlink, so installation and service commands still run in the project
directory. The project-local `./web-search` command remains available as a fallback.

The first launch installs the console's small Node.js dependency set. Inside the console you can:

- Install or update the Python application and Chromium runtime.
- Launch Docker Desktop when necessary and start, stop, or restart SearXNG.
- See Docker, SearXNG, the model strategy, Chromium, and MCP readiness at a glance.
- Run the full readiness doctor and follow SearXNG logs.
- Select or disable a dedicated evidence model from every model exposed by the configured local
  OpenAI-compatible endpoint.
- See whether the optional semantic reranker is reachable; failures retain deterministic ranking.

The console does not replace or modify Pi. Pi continues to launch the single MCP server over stdio
when it needs `web_search`; the console is only an operator interface for installation and local
service management.

## Requirements

- Python 3.11 or newer.
- Node.js 22.19 or newer for the optional terminal console.
- Docker (recommended for SearXNG), or another SearXNG instance with JSON enabled.
- A Pi MCP adapter/extension with MCP sampling support.
- Optionally, an OpenAI-compatible chat-completions endpoint as a direct fallback.

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

No model ID is required when Pi connects through `pi-mcp-adapter`. The server requests model work
through MCP sampling, and the adapter selects the model active in the current Pi session. Changing
models in Pi therefore changes the research model without restarting or editing this server.

Optionally, choose **Configure evidence model** in the `web-search` terminal console. The console
lists every model returned by the local endpoint's `/models` response without recommending or
filtering the list. The selected model analyzes retrieved pages; request compilation, search
planning, gap assessment, and final synthesis continue to use Pi's active model. The selection is
stored in `.web-search-data/config.json`, so Pi's MCP configuration remains model-free. If the
dedicated model fails, the current Pi model handles that page; repeated failures disable the
dedicated route for the remainder of the search.

Sampling normally shows approval dialogs. Because one research run can make several model calls,
the practical configuration for this trusted local server is:

```json
{
  "settings": {
    "sampling": true,
    "samplingAutoApprove": true
  }
}
```

`samplingAutoApprove` applies to every MCP server in the same adapter configuration, so keep this
server in a trusted project scope. Leave it `false` if you prefer to approve every request and
response manually.

The `.env` file is optional. The terminal console and MCP server load it automatically from the
project directory; environment variables explicitly supplied by Pi or the operating system take
precedence. Use it for SearXNG settings, local model authentication, or a direct model fallback for
MCP clients that do not support sampling:

```bash
cp config.example.env .env
```

Important settings:

| Variable | Default | Purpose |
|---|---|---|
| `WEB_SEARCH_SEARXNG_URL` | `http://127.0.0.1:8080` | SearXNG base URL |
| `WEB_SEARCH_MODEL_BASE_URL` | `http://127.0.0.1:8000/v1` | Optional direct-fallback API base |
| `WEB_SEARCH_MODEL_ID` | none | Optional direct-fallback model ID |
| `WEB_SEARCH_EVIDENCE_MODEL_BASE_URL` | saved endpoint or model base URL | Override the dedicated evidence-model API base |
| `WEB_SEARCH_EVIDENCE_MODEL_ID` | saved selection or none | Override the dedicated evidence model selected in the terminal UI |
| `WEB_SEARCH_EVIDENCE_MODEL_MAX_TOKENS` | `1600` | Maximum output for page-evidence extraction |
| `WEB_SEARCH_RERANKER_MODEL_ID` | none | Optional model served through native `POST /v1/rerank` |
| `WEB_SEARCH_RERANKER_BASE_URL` | model base URL | Reranker endpoint; compatible with oMLX and Cohere/Jina-style APIs |
| `WEB_SEARCH_RERANKER_MIN_RELEVANCE_SCORE` | `0.08` | Raw semantic eligibility floor before a page may be fetched |
| `WEB_SEARCH_RERANKER_RELATIVE_RELEVANCE_RATIO` | `0.15` | Reject results far below the best semantic result in a batch |
| `WEB_SEARCH_LEXICAL_MIN_RELEVANCE_SCORE` | `0.01` | Conservative eligibility floor when the reranker is unavailable |
| `WEB_SEARCH_PREFETCH_PAGES` | `2` | Concurrent page retrieval window; model inference remains sequential |
| `WEB_SEARCH_READ_URL_MAX_CHARS` | `60000` | Server ceiling for extracted characters returned by one `read_url` call |
| `WEB_SEARCH_READ_URL_MAX_LINKS` | `100` | Maximum extracted links returned by one `read_url` call |
| `WEB_SEARCH_DATA_DIR` | `.web-search-data` | SQLite cache and traces |
| `WEB_SEARCH_ALLOW_PRIVATE_URLS` | `false` | Development-only reader override |
| `WEB_SEARCH_ALLOW_PROXY_FAKE_IPS` | `false` | Permit hostname-only `198.18.0.0/15` answers from a local TUN proxy |
| `WEB_SEARCH_ENABLE_CRAWL4AI` | `true` | Escalate incomplete pages to Chromium |

Do not enable private URL fetching for normal use. SearXNG and any direct fallback model server have
their own explicitly configured local endpoints; public result pages remain protected against SSRF.
If a local proxy returns synthetic `198.18.0.x` DNS answers for every public hostname, enable
`WEB_SEARCH_ALLOW_PROXY_FAKE_IPS`. This exception does not permit literal fake-IP URLs or any other
private, loopback, link-local, or metadata range.

Candidate eligibility is separate from candidate ordering. When the semantic reranker is available,
the controller rejects results below its raw relevance floor before HTTP or browser prefetch begins;
SearXNG rank and source-diversity bonuses cannot override that decision. If a whole result batch is
rejected, the controller searches again for the unresolved requirement and relaxes the floor across
later attempts. After several weak batches it may probe one best candidate to preserve recall for
obscure topics. Debug traces record rejected URLs, scores, gate mode, and the effective threshold.

## 4. Check the services

```bash
.venv/bin/web-search-doctor
```

The command verifies the SearXNG JSON API, Crawl4AI package and Chromium runtime, the selected model
strategy, and the data directory. It checks `/models` only when a direct fallback model is configured;
the active Pi model can only be verified during an MCP call.

## 5. Connect Pi

Point your Pi MCP extension/adapter at the absolute executable:

```text
/Users/donais/Documents/Projects/web-search/.venv/bin/web-search-mcp
```

Start from [integrations/pi/mcp-server.example.json](integrations/pi/mcp-server.example.json). It
enables automatic sampling approval for this trusted local scope and contains no model ID. Pi MCP
adapters differ in their outer configuration format, but the command and environment are the same.

The stdio executable intentionally uses MCP's 2025-11-25 handshake protocol. Iterative research
needs several server-to-client sampling requests during one tool call, while the 2026-07-28 protocol
requires sampling to be represented as multi-round input. `pi-mcp-adapter` negotiates automatically
and falls back to this compatible handshake path.

The tool signatures are:

```text
read_url(url, render="auto", cursor=0, max_chars=4000, include_links=false)
web_search(query, effort="auto", freshness=null)
```

Use `read_url` when the URL is already known. Its `auto` rendering mode uses the shared HTTP,
Trafilatura, basic-HTML, and conditional Chromium stack; `never` prevents Chromium from launching,
and `always` attempts Chromium while preserving the HTTP result if browser rendering fails. Direct
page output includes the HTTP status, a separate semantic page status, and warnings for likely soft
404s, CDN/upstream error documents, or incomplete browser output. Content is returned in bounded
inline chunks; continue with `next_cursor`, and use a smaller `max_chars` with `include_links=false`
for batched or fan-out calls.

Send a complete research request to `web_search` in one call. The server permits one active research
run at a time and rejects overlapping calls rather than letting two searches contend for the same
local model.

Pi should pass the user's temporal wording faithfully. For requests such as "latest", "recent",
"current", or "today", it must not insert a calendar year unless the user supplied one. The server
anchors those relative terms to its local date and includes that authoritative date in every model
stage. Explicit requests such as "news from 2025" remain unchanged.

- `quick`: a short lookup with a five-minute wall-clock ceiling.
- `auto`: normal research and comparisons with a twelve-minute wall-clock ceiling.
- `thorough`: a wider evidence search with a twenty-five-minute wall-clock ceiling; it does not stop for saturation
  before attempting at least three searches and collecting usable evidence from six domains.

These modes set maximum wall-clock, active-browsing, search-call, and page budgets. Model inference
and approval latency count toward the wall-clock ceiling but not the narrower browsing allowance;
each individual model call also uses `WEB_SEARCH_MODEL_TIMEOUT_SECONDS`. The controller reserves
time for final synthesis so it can return the best available evidence before Pi's outer timeout.
It reads only a small candidate batch from each query before moving to another search angle, and it
defers model-based gap assessment until a new query is actually needed.

Evidence claims are accepted only when their statement is lexically supported by a verbatim page
excerpt and every numeric/date token appears in that excerpt. Invalid excerpts may be replaced from
the page, but replacement lowers confidence; unsupported claims are discarded. Source classes are
model-generated descriptive metadata only. They are not validated as official ownership and never
affect ranking, coverage, or stopping decisions; those depend on supported claims and distinct
source domains.
Canceled calls are finalized in the run ledger with a `cancelled` event at any controller stage.

## Development

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

The test suite is offline. It exercises the controller, evidence rules, citations, reader byte bounds,
URL safety, SearXNG, MCP sampling and direct model-server adapters, Crawl4AI failure handling, and
the MCP tool schema without requiring live SearXNG or model services.

Replay the bundled deterministic evidence fixtures after changing prompts, coverage rules, or
retrieval behavior:

```bash
.venv/bin/web-search-eval
```

The replay reports accepted claims, requirement coverage, unresolved gaps, conflicts, and candidate
gate decisions without depending on live search results. One bundled noisy-result fixture verifies
that a plausible high-ranked news article is rejected before page reading.

## Security defaults

- Only HTTP(S) result URLs are accepted.
- Loopback, private, link-local, multicast, and metadata destinations are blocked.
- DNS and redirect destinations are checked on every hop.
- Crawl4AI installs a browser route guard that also blocks private subresources and redirects.
- Credentials in URLs and oversized responses are rejected.
- Browser sessions, authentication, form submission, shell access, and arbitrary JavaScript are not
  available to the research model.
- Retrieved pages are delimited as untrusted evidence and cannot change the research policy.
