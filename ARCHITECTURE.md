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

The caller procedure lives in packaged `research_workflow.md`. `workflow.py` supplies the tool-level
contract and guidance derived from retrieval status, source truncation, and engine attribution.
These are suggestions, not a confidence score or stopping gate. The caller keeps a compact evidence
note in its conversation and continuation summaries. It ends with supported completion, unproductive
available search paths, or a blocked/limited partial result, preserving outstanding gaps.
Pi can load `integrations/pi/workflow.mjs` to inject the shared procedure whenever both direct tools
are active. It adds no tools, model calls, or external storage and respects other system instructions.

`web_search` has a configurable 30-second default deadline covering dispatch queue and upstream
request time. Dispatches are serialized so each provider instance restores cooldowns recorded by
its predecessor. The provider is configured with zero retries and an explicit general-web engine
pool. It returns cached results or performs one request, without diversity widening or page reads.
The caller may choose one engine from that pool with `engine`. The selection is part of
cache identity; selection cannot bypass cooldowns or introduce unconfigured engines.
Cancellation propagates and closes the HTTP client. Successful empty searches are distinguished
from backend failures; partial upstream failures preserve returned results and diagnostics.
Search caches retain the original retrieval timestamp and warnings, including skipped engines.
Current engine cooldowns are reported separately so recovery does not erase a cached result's
limited retrieval conditions. Legacy cache rows explicitly report missing provenance.

`read_url` keeps the existing shared, bounded reader runtime, document safety, browser actions,
visual output, and snapshot pagination. Optional query-focused extraction chooses a content window;
it does not score evidence or claim that the page answers the question. Publication dates and page
status remain metadata for the calling model to inspect.
The doctor performs a cache-bypassing public documentation read through this same reader stack,
in addition to checking search and installed components. HTTP extraction resolves links against
the final page URL and HTML base before converting to Markdown.

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

`research_benchmark.py` is an opt-in evaluation runner, outside the production path. An isolated Pi
caller receives only the two actual MCP schemas. Controlled cases replace search/HTTP network
boundaries while retaining tool validation, reading, cache behavior, and outputs. Separate live cases
use the real network. Authored references check requested facts against quoted passages actually read;
final prose and the quality of the stopping rationale still require review. Time/call ceilings stop
evaluation runaway and fail the run; they are not evidence thresholds.
