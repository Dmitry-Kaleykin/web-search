# Architecture

The calling model owns research reasoning. The MCP server supplies discovery and source access.

```text
Calling model
  |-- web_search(query, language, time_range, page, limit, refresh)
  |      |-- bounded dispatch queue
  |      |-- SQLite search cache and persisted engine cooldowns
  |      |-- one SearXNG request, pinned to available configured engines
  |      `-- ranked search results and retrieval diagnostics
  |
  |-- read_url(selected URL, ...)
  |      |-- URL/DNS/redirect safety and shared retrieval limits
  |      |-- HTTP + Trafilatura / document parsing
  |      |-- conditional Chromium rendering and read-only controls
  |      `-- immutable pageable text, metadata, links, optional images
  |
  `-- chooses follow-ups, evaluates evidence, and writes the final cited answer
```

## Boundaries

`server.py` exposes exactly `web_search` and `read_url`. It does not import or instantiate the
research controller, agent, model clients, or reranker. There is no server-side query planning,
claim extraction, coverage calculation, semantic relevance gate, source-count rule, or synthesis.
The model interprets untrusted source content and is responsible for its final answer.

`web_search` has a configurable 30-second default deadline covering dispatch queue and upstream
request time. Dispatches are serialized so each provider instance restores cooldowns recorded by
its predecessor. The provider is configured with zero retries and an explicit general-web engine
pool. It returns cached results or performs one request, without diversity widening or page reads.
Cancellation propagates and closes the HTTP client. Successful empty searches are distinguished
from backend failures; partial upstream failures preserve returned results and diagnostics.

`read_url` keeps the existing shared, bounded reader runtime, document safety, browser actions,
visual output, and snapshot pagination. Optional query-focused extraction chooses a content window;
it does not score evidence or claim that the page answers the question. Publication dates and page
status remain metadata for the calling model to inspect.

## Compatibility

Tool names and the `read_url` schema are retained. The `web_search` input and output schemas change:
`effort` and `freshness` are replaced by search controls; `results` replace `answer_markdown`,
coverage, research traces, and synthesis outcomes. Clients must reload tool schemas after restart.
Sampling capability, sampling approval, model endpoints, and reranker endpoints are unnecessary.
The doctor and console report retrieval readiness without probing model services.

The old research modules and deterministic evaluation fixtures remain available for historical
comparison and direct library callers, but are unreachable from the public MCP workflow. Their
configuration fields do not control the two tools. The [old design](docs/legacy-research-architecture.md)
is archived separately. See [README.md](README.md) for setup, API fields, and resource limits.
