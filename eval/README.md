# Retrieval and historical research evaluations

## Caller research benchmark

From the project root, with Pi configured for the model you intend to use:

```sh
.venv/bin/python -m web_research.research_benchmark run \
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
.venv/bin/python -m web_research.research_benchmark run --live \
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

The [Stage 2 baseline](baselines/2026-09-18-stage2.md) records the local caller comparison, live check,
and limitations found by reviewing the generated answers.

## Live retrieval baseline

Run from the project root with SearXNG running and the reader configured:

```sh
.venv/bin/python -m web_research.retrieval_baseline \
  --output eval/baselines/$(date +%Y-%m-%d)-retrieval.json
```

This is an opt-in network check, separate from the offline research fixtures. It uses the public
MCP tools in-process with no model, performs two focused searches (English/Russian), reads official
Python documentation and public package JSON, and checks search-cache provenance and immutable
read continuation. Cases are in `retrieval_cases.json`; `--cases` selects another manifest.
It records exact tool arguments, outputs, timings, retrieval warnings, and per-case checks.
Initial calls request fresh data. Follow-up calls specifically exercise cache and pagination.

The exit code reports mechanical check failures. It does **not** score relevance, factual answers,
source independence, or stopping decisions. Review the returned sources using each case's review
prompt. Empty, blocked, or changed live sources remain recorded failures rather than being retried
until the report turns green. Reports contain public result snippets and bounded page excerpts;
inspect them before sharing if you replace the bundled cases with private queries.

The Stage 1 assessment and recorded run are in [baselines](baselines/2026-09-18-stage1.md).

## Offline and historical checks

The public MCP workflow now returns search results and page content to the calling model.
Evidence-ledger fixtures below exercise the retained historical research library, not a validation
step performed by `web_search` or `read_url`. The current tool contract is tested in
`tests/test_server.py`; reader integrations still apply to the public workflow.

Run deterministic evidence cases:

```sh
.venv/bin/web-search-eval
```

Run the local retrieval integrations too:

```sh
.venv/bin/web-search-eval --integration
```

The integration command needs the `dev` and `browser` extras, installed Chromium, permission to
bind a localhost test server, and Tesseract for the real OCR case. It makes no search-engine or
model API calls. Without Tesseract, the OCR integration is explicitly skipped; the missing-OCR
fallback is still tested. Install Tesseract with `brew install tesseract` on macOS or the system
package manager on Linux. The default OCR language is English.

| Failure | Evaluation and acceptance criterion |
|---|---|
| Stale/future evidence | Fixed `as_of_date`; out-of-window articles contribute zero accepted claims. |
| Syndication | Identical/near-identical reports count as one family across different domains. |
| Unsupported answers | Negation, qualifications, actor order, numbers, missing citations and incorrect source associations are rejected. |
| Cache freshness | An explicit refresh reaches the source; creating a cache variant cannot renew old evidence. |
| Pagination | Reassembled chunks equal one extraction, even after the source changes and the store reopens. Expired or mismatched cursors fail explicitly. |
| Duplicate work | Concurrent identical reads share a fetch; cancelling one waiter preserves the others; cancelling the last waiter stops retrieval. |
| Documents | A generated PDF retains page numbers and values; a scanned PDF is OCR'd; page images remain available without OCR. |
| Difficult pages | A local JavaScript article renders; tabs/load-more controls expose changed content; an unrelated destructive control is refused; soft errors remain marked. |

A passing adversarial JSON fixture means the expected rejection happened. It does not mean an
incorrect answer was accepted. The `answer_valid` field in JSON output distinguishes these cases.
The historical controller cases supply fixed evidence so their outcomes are repeatable.

To add a case, copy a JSON fixture, use a fixed `as_of_date`, and set exact expectations such as
`accepted_claims`, `source_count`, `sufficient`, `unresolved_gaps`, and `answer_valid`. Tests for
actual fetching and browser behavior belong in `tests/test_reliability.py`,
`tests/test_documents.py`, or `tests/test_browser_integration.py`.

## Live research quality

Local tests establish retrieval and validation behavior; they do not measure recall across the
live web. For an end-to-end check in the connected main-model session, use the cases in
`live_cases.json`. Let the calling model run each request using `web_search` and `read_url`, record the tool calls
and final answer, and manually check the cited passages. Record supported/unsupported factual statements, required
items covered, independent source families, elapsed time, fetched pages, and unnecessary retries.
Do not turn unstable live answers into fixed unit-test expectations. Date-sensitive questions use
an explicit date supplied at execution time; record that date with the run.

No second model or external judge is required. A human-reviewed source passage is the reference
for live answer quality; the model's own confidence or a valid citation ID is not a correctness label.
