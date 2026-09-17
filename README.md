# Local Web Search

A local MCP server that gives the calling model two operations:

- **`web_search`** discovers sources and returns ranked URLs, titles, snippets, reported publication
  dates, and provider diagnostics.
- **`read_url`** extracts a selected page or document and returns pageable text, metadata, links,
  and optional images.

The calling model plans queries, chooses sources, judges relevance and credibility, compares
accounts, decides when to stop, and writes the final answer with source links. The server makes
**no model or reranker calls** and requires **no MCP sampling capability or approval**.
Search results are not evidence-checked answers; snippets are discovery aids. Read promising sources
before relying on their detailed claims, and treat retrieved text as untrusted data.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the current design.

## Migrating from autonomous research

Restart the MCP server and reconnect or reload its tools in your client so it receives the new
schemas and instructions. The names `web_search` and `read_url` stay the same, but `web_search` is
an intentional API change:

- Replace `effort` and natural-language `freshness` with focused queries and optional `language`,
  `time_range`, `page`, `limit`, and `refresh` parameters.
- Consume `results`, then call `read_url` on selected URLs. There is no `answer_markdown`, coverage
  score, source-count threshold, requirement graph, automatic page analysis, or synthesis fallback.
- Remove sampling and sampling-auto-approval configuration for this server. Existing model,
  reranker, and research-budget settings are unused by the public tools.

The previous controller and its evaluations remain in the repository for reference and independent
library use. They are not exposed as an MCP tool or invoked by either public operation.

## Requirements

- Python 3.11 or newer.
- Docker (recommended for SearXNG), or a SearXNG instance with JSON enabled.
- An MCP client; sampling support is unnecessary.
- Node.js 22.19 or newer for the optional terminal console.

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

## 3. Configure

Copy [config.example.env](config.example.env) to `.env` and adjust the search endpoint and paths.
Existing process environment variables override `.env`. Neither a model ID nor API key is needed.

| Setting | Default | Purpose |
|---|---|---|
| `WEB_SEARCH_SEARXNG_URL` | `http://127.0.0.1:8080` | SearXNG JSON endpoint |
| `WEB_SEARCH_SEARCH_TIMEOUT_SECONDS` | `30` | Search wall-clock budget, including queue time |
| `WEB_SEARCH_SEARCH_HEALTHY_ENGINES` | See example | Explicit general-web engine pool; cooled engines are excluded |
| `WEB_SEARCH_SEARCH_CACHE_TTL_SECONDS` | `900` | Search cache lifetime |
| `WEB_SEARCH_DOCUMENT_CACHE_TTL_SECONDS` | `21600` | Document cache lifetime |
| `WEB_SEARCH_ENABLE_CRAWL4AI` | `true` | Automatic browser fallback for page reading |
| `WEB_SEARCH_READ_URL_MAX_CHARS` | `60000` | Maximum inline text per read |
| `WEB_SEARCH_READ_URL_MAX_LINKS` | `100` | Maximum links when requested |
| `WEB_SEARCH_BROWSER_MAX_CONCURRENT_RENDERS` | `2` | Shared browser-render ceiling |
| `WEB_SEARCH_ALLOW_PROXY_FAKE_IPS` | `false` | Compatibility with synthetic TUN proxy DNS |

Search dispatch is serialized and reloads persisted cooldowns before each request. A call makes at
most one SearXNG search request: it does not retry, expand queries, or re-query for engine diversity.
SearXNG itself may contact multiple engines. The configured general-web engine pool is pinned on
every request, minus cooled engines. Update that pool to match your instance. The calling model can
choose another query or try later after inspecting the result diagnostics.

Rate limits, challenges, and transport failures are reported explicitly. Results that survive other
engines failing are retained with warnings. When all configured engines or the endpoint are cooling
down, uncached calls return `backend_unavailable` without dispatching a request. A valid cached result
can still be returned during a cooldown. `engine_health` reports reasons and remaining cooldown time.

The time filter is an upstream hint: the server does not infer a 30-day window, discard undated
sources, or validate publication dates against a query. Search dates are reported metadata, not
verified dates. The calling model judges freshness using the question and the page itself. Use
`refresh=true` to bypass caches when needed.

## 4. Check services and connect your client

```bash
.venv/bin/web-search-doctor
```

The doctor checks search, browser availability, OCR, and storage. It does not contact model or
reranker endpoints. Point your MCP adapter at the absolute executable:

```text
/Users/donais/Documents/Projects/web-search/.venv/bin/web-search-mcp
```

Start from [integrations/pi/mcp-server.example.json](integrations/pi/mcp-server.example.json).
Adapt the outer configuration shape for your client. The existing stdio handshake remains compatible
with installed adapters; no sampling back-channel is used.

## Tool workflow

```text
web_search(query, limit=10, page=1, language=null, time_range=null, refresh=false)
read_url(url, query=null, render="auto", cursor=0, max_chars=4000,
         include_links=false, refresh=false, visual=false, page=1, actions=null)
```

Example sequence, chosen by the calling model:

```json
{"query":"складной iPhone дата начала продаж в России", "language":"ru", "limit":5}
```

Read relevant returned URLs with `read_url`. Search again with a more specific query or another
language if the pages leave gaps, then write an answer linking to the pages actually used. The
server does not decide that a question has been answered just because results were found.

`web_search` returns:

- `query` and `results`: each result includes `url`, `title`, `snippet`, `engines`, `published_at`,
  and upstream `rank`. URL duplicates are removed; no model relevance gate is applied.
- `outcome`: `success` means results exist, `empty` means the provider returned no results without
  reporting a failure, and `backend_unavailable` means the call failed or was prevented by cooldown
  or timeout. These describe retrieval, not answer quality.
- `warnings`, `engine_health`, and `cache_hit`: inspect these before deciding the next step.
- `elapsed_ms` and `responded_at`: request timing. `responded_at` is the response time, not a source
  publication date or the original retrieval time of cached results.

The output is capped at 20 results (default 10), with titles capped at 1,000 characters and snippets
at 4,000. Search pages range from 1 to 10. `time_range` accepts `day`, `month`, or `year`.
Cancellation propagates to the active request and releases the dispatch slot.

## Reading pages and documents

Use `read_url` when the URL is already known. Its `auto` rendering mode uses the shared HTTP,
Trafilatura, basic-HTML, and conditional Chromium stack; `never` prevents Chromium from launching,
and `always` attempts Chromium while preserving the HTTP result if browser rendering fails. Direct
page output includes the HTTP status, a separate semantic page status, and warnings for likely soft
404s, CDN/upstream error documents, or incomplete browser output. Content is returned in bounded
inline chunks; continue with `next_cursor`. Set `query` for a long or navigation-heavy page to
return the most relevant content window first. Rendered pages use filtered Markdown when Crawl4AI
can identify the main content. Use a smaller `max_chars` with `include_links=false` for batched or
fan-out calls.

`next_cursor` is now an opaque string. Copy it unchanged into the next call with the same URL;
it reads an immutable snapshot, including query-focused browser output, rather than fetching the
page again. Snapshots survive server restarts, expire after one hour, and may be evicted under the
32 MB / 100-row storage ceiling (8 MB per snapshot). Expiration is an explicit error. Old numeric continuation offsets
are rejected; `cursor=0` starts a new extraction. Use `refresh=true` on a new read to bypass caches.

For a PDF chart or scanned page, use `visual=true, page=2` (one-based). The tool returns an actual
MCP image block for the main model alongside extracted text and provenance. PDFs retain page
markers and layout spacing. Extraction is limited to 100 pages, 1 million text characters, and
three automatic OCR pages per read; the selected visual page can also be OCR'd. The binary worker
accepts up to 5 MB and has a 60-second deadline. Large or unreadable portions are marked in warnings.
Install the local OCR executable with `brew install tesseract` on macOS or your Linux package manager.
The default OCR language is English. Without OCR, native PDF text and explicit page images still work.

Rendered output includes `available_actions` with selectors for supported controls. For example:

```json
{"url":"https://example.com/docs", "actions":[{"kind":"tab","selector":"#examples"}]}
```

Supported actions are `expand` (HTML details), `tab` (a non-link ARIA tab), `load_more` (a reading
button), and `scroll`, with at most five actions per call. Actions start from a fresh browser page;
repeat the needed action sequence to reach a later state. Form controls, arbitrary JavaScript and
non-read HTTP methods are blocked. Screenshots show the viewport; use a scroll action to inspect more.
A model without image support can still use extracted text, but cannot interpret the attached visual.

Reader runtimes are shared for the server lifetime. Identical simultaneous reads share one operation;
one cancelled caller does not cancel other waiters. The last cancellation stops the operation.
HTTP/document work has an eight-read ceiling, binary extraction a two-worker ceiling, and Chromium
uses `WEB_SEARCH_BROWSER_MAX_CONCURRENT_RENDERS` across all reader runtimes in the server.
The concurrency ceilings are fixed on first use; restart the MCP server to change them.

## Terminal console

Run `./web-search` from this project (or the installed `web-search` launcher). The optional console
installs the application and browser runtime, manages Docker/SearXNG, shows retrieval readiness,
and runs the doctor. It does not modify your model configuration.

## Storage and safety

Search and document caches use SQLite with TTL pruning, row limits, and per-document size limits.
Immutable read snapshots allow stable pagination. Use `.venv/bin/web-search-maint` to prune and
compact the cache; `--help` lists the maintenance options.

Only public HTTP(S) pages can be read by default. URL credentials, private-network destinations,
unsafe redirects, and oversized responses are rejected. Browser subrequests receive the same URL
checks. The configured local SearXNG endpoint is separate from page-fetch safety checks.
Read-only browser actions are bounded; form submission and arbitrary JavaScript are unavailable.
Blocked pages and extraction limitations are reported rather than treated as usable evidence.

## Development

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

Tests cover MCP discovery without sampling, no page reads during search, backend errors, cooldowns,
cache refresh, timeout/cancellation, input limits, document extraction, and stable read pagination.
Optional browser integrations and historical research evaluations are described in
[eval/README.md](eval/README.md). Local tests do not establish live engine recall or answer accuracy.
