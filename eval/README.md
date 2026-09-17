# Retrieval and historical research evaluations

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
