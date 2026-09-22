# Evaluating the current search-and-read workflow

## Caller research benchmark

With Pi configured for the model you intend to use:

```sh
.venv/bin/web-search-eval research \
  --directory /tmp/web-search-research-current
```

This explicitly invokes the user's configured Pi model. A local model keeps the run local; a hosted
Pi provider uses that provider's normal billing. The production server never invokes a model.
Use a new output directory per run. `--case simple_fact` selects a case, `--policy previous` selects
the recorded Stage 1 instructions/descriptions and omits Stage 2 result guidance and engine controls.
The comparison isolates the caller workflow, not old extraction bugs fixed in Stage 1.

Six controlled cases exercise an authoritative fact, comparison with negation, irrelevant initial
results, conflicting applicable sources, access failure, and unproductive searches. Each has authored
source content and reference values. The caller sees the question and tools, not the reference answers.
It chooses queries, page reads, and stopping itself. Fixtures replace only the network boundaries;
actual MCP schemas, validation, search caching, HTML extraction, and read outputs remain in use.
The evaluation adapter serializes its subprocess calls to avoid SQLite initialization races; this is
not a production concurrency or latency benchmark.

An explicit live case exercises the real search service and public Python documentation:

```sh
.venv/bin/web-search-eval research --live \
  --directory /tmp/web-search-research-live
```

The runner starts an ephemeral Pi session with only these two tools, disables unrelated extensions,
skills, context files, and built-in shell/filesystem tools, and supplies the shared workflow. It does
not change the user's selected model. The default 180-second and 20-call ceilings are configurable
with `--max-seconds` and `--max-calls` (calls reaching the MCP bridge). Invalid arguments rejected by
Pi before dispatch are counted separately in attempted calls and are bounded by the time ceiling.
Hitting a ceiling leaves an incomplete failed run, never a
successful research result. No research-success rule counts pages or calls.

Each case records `calls.jsonl`, `result.json`, caller events/errors, and the delivered tool protocol.
The summary reports authored fact checks, citations matched to passages actually returned, required
source coverage, stop category, exact repeated MCP calls, total attempted calls including validation
failures, and resource failures. Reports may recover JSON
from prose/fences or separate stop blocks; a `format_warning` records that deviation. Additional
findings are flagged for review, not assumed correct merely because a required fact is correct.
Quoted passages are checked in all reported findings, including additional ones; this detects
fabricated quotation text without pretending to judge whether an arbitrary claim follows from it.
Review the full answer for unsupported additions, missing qualifications, and whether the stopping
reason is justified. The checks are not an independent semantic judge. Synthetic references are
authored test facts; live reference passages can change and must be reviewed when checks fail.

The saved previous policy comes from the revision named in `research_previous_policy.json`.
Keep raw caller logs local: they can contain model reasoning. Publish reviewed answers and tool traces
instead. A smoke run is useful evidence about these cases, not a broad accuracy or efficiency claim.

New generated baselines and private session reviews stay local, excluded from version control.
The checked-in Stage 1 baseline is a historical retrieval record, not a requirement for running the
benchmarks or evidence of current answer accuracy.

## Live retrieval baseline

Run from the project root with SearXNG running and the reader configured:

```sh
.venv/bin/web-search-eval retrieval \
  --output eval/baselines/$(date +%Y-%m-%d)-retrieval.json
```

This is an opt-in network check, separate from controlled caller research. It uses the public
MCP tools in-process with no model, performs two focused searches (English/Russian), reads official
Python documentation and public package JSON, and checks search-cache provenance and immutable
read continuation. Cases ship in `src/web_research/benchmark_data/retrieval_cases.json`; `--cases` selects another manifest.
It records exact tool arguments, outputs, timings, retrieval warnings, and per-case checks.
Initial calls request fresh data. Follow-up calls specifically exercise cache and pagination.

The exit code reports mechanical check failures. It does **not** score relevance, factual answers,
source independence, or stopping decisions. Review the returned sources using each case's review
prompt. Empty, blocked, or changed live sources remain recorded failures rather than being retried
until the report turns green. Reports contain public result snippets and bounded page excerpts;
inspect them before sharing if you replace the bundled cases with private queries.

The exit status and saved results describe this run only; upstream engines and sources can change.

## Offline public-tool checks

```sh
.venv/bin/web-search-eval check
# No arguments also selects check.
```

This is the console's “Test search and reading (offline)” action. It makes calls through the two
actual MCP tools with fixture search and HTTP responses. It checks discovery, source text, cache
provenance, explicit engine selection, invalid engine rejection, backend failure, and unavailable
pages. It invokes neither network services nor a model and requires no development extras.
Failures produce a nonzero exit status. These checks do not grade coverage, source independence,
claim support, freshness judgments, or stopping decisions.

All required manifests and the Pi adapter ship in `web_research/benchmark_data/`. Defaults resolve
relative to the installed package, not the working directory. Custom live retrieval manifests can be
supplied with `--cases`; caller research cases and authored references are maintained in the package.

## Reader integration tests

From the checkout with the development and browser extras installed:

```sh
.venv/bin/pytest -q
WEB_SEARCH_RUN_BROWSER_TESTS=1 .venv/bin/pytest -q tests/test_browser_integration.py
node --test integrations/pi/workflow.test.mjs
```

The browser tests need installed Chromium and permission to bind a local test server. They use local
pages and make no search-engine or model calls. Document tests also exercise local PDF extraction,
page images, and OCR when Tesseract is installed; missing OCR is reported as a skip. Cache refresh,
continuation snapshots, cancellation, URL safety, and malformed pages remain covered by the active
retrieval tests. Retired evidence-ledger fixtures are no longer an evaluation mode; their source and
tests are available through the [historical design note](../docs/legacy-research-architecture.md).

## Reviewing research quality

For additional manual exercises, [live_cases.json](live_cases.json) lists prompts requiring evaluator
choices; it is not an automated benchmark manifest.

Review the complete final answer against the pages actually read, including dates, scope, negation,
qualifications, and units. Check that cited URLs were opened and that recommendations and forecasts
have supporting evidence or are clearly identified as inference. For time-sensitive answers, record
the run date and distinguish it from each source's publication date and the event date.

Review whether follow-ups addressed outstanding gaps and whether the final stop reason fits the
observed evidence and access limitations. Passing retrieval checks or authored benchmark cases does
not establish broad live-web recall, general factual accuracy, or appropriate stopping on new tasks.
A human-reviewed source passage is the reference for live answer quality; the caller's confidence
and a valid-looking citation are not correctness labels.
