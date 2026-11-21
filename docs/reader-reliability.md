# Reader reliability review

Reviewed 17 November 2026. The two-tool, local-extraction design is sound, but it cannot promise
complete access to every website. Extraction quality and permission to fetch content are separate
problems. No model, paid extraction API, CAPTCHA solver or extra MCP tool was added in this review.

## What the character limit means

`read_url` defaults to **4,000 characters per reply**, configurable by the caller up to 60,000
(and bounded by `WEB_SEARCH_READ_URL_MAX_CHARS`). This keeps simultaneous reads from overwhelming
an agent's context. It is not a 4,000-character download or parsing limit.

HTTP responses are bounded at 5 MB of decoded bytes. HTML/browser extraction normally retains up
to one million characters. JSON is not character-capped before pagination, to avoid corrupting the
stored structure; its fetched bytes remain bounded. PDFs have separate limits: 100 pages and
three automatic OCR pages, with an optional selected page image. A continuation snapshot expires
after one hour and can be evicted under the existing storage limits.

Use `next_cursor` for following text. A query-focused first reply may skip earlier context;
`start_cursor` now returns to the beginning of **that same snapshot**, without refetching or
rerunning query filtering. `content_characters` measures retained extraction, not the full website.
Extraction-cap warnings mean discarded text is unavailable even through continuation.

## Parsing and selection

The inexpensive path is HTTP → content-type handling → Trafilatura main-content extraction, with
a basic HTML fallback. Documents such as JSON, text, feeds, PDFs and images have their own paths.
Strong shell/challenge signals can escalate HTML to local Chromium through Crawl4AI. Tabs,
details, load-more and scroll remain bounded read-only actions.

This is heuristic extraction, not an LLM summary. Trafilatura uses document structure; the query
window uses overlapping text windows, query-term coverage and density, with nearby headings for
context. Link destinations are excluded from window ranking where recognized, so repetitive URL
paths do not outrank visible prose. These choices help navigate a page; they do not certify that
the text answers the question.

## Problems fixed

| Problem | Resulting behavior |
|---|---|
| Browser pruning/BM25 discarded sections before snapshotting | Retain raw browser Markdown; focus only the returned window. Qualifications remain pageable. |
| A matching H1 could hide a better passage far below | Scan the retained text for a better matching window; preserve the introduction when the H1 covers the whole query. |
| Jaccard similarity shrank as pages grew | Check query-term presence before launching a browser. Complete long pages no longer escalate merely because they contain many different words. |
| Successful browser calls could return worse content | Preserve usable HTTP text when browser output is an error, empty or a detected shell. |
| Temporary blocks could become six-hour cached results | Skip known challenges, errors, shells and failed/rejected browser fallbacks in normal document caching. |
| HTTP HTML errors lost their body and status diagnostics | Return bounded HTML diagnostic text and status; detect Cloudflare's challenge header and narrowly matched challenge prompts/titles. |
| Plain text/XML were parsed as HTML | Preserve whitespace, tags and literal entities; support XML feed media types. |
| HTML meta charsets were ignored | Honor declared HTML encodings; warn about destructive decoding. |
| Extraction caps could still produce `page_status=ok` | Mark known extraction loss as incomplete, separately from ordinary reply pagination. |
| Browser redirect metadata described the first hop | Use Crawl4AI's final URL and final-response status when available. |
| Two async browser-startup tests silently did no work | Execute them as async tests and repair their outdated doubles/assertions. |

Old extraction cache variants are bypassed. Existing immutable snapshots keep their original text
until expiry. The output adds `start_cursor`; existing inputs and the two tool names remain intact.
An HTTP HTML error now produces a diagnostic result with `page_status=suspected_error`, rather
than necessarily raising an MCP error. Callers must inspect page status as well as tool errors.

## Verification

Automated regressions cover full-snapshot pagination and restart, deep query matches, long-page
browser selection, plaintext/CSV/Markdown/XML fidelity, legacy Cyrillic encoding, challenge
recovery and cache behavior, browser-output rejection, and redirect metadata. The real local
Chromium tests exercise JavaScript content, screenshots, tabs and load-more, including refusal
of unsupported controls.

A live smoke check used the development reader stack, bypassing caches:

| Resource | Observation |
|---|---|
| Python asyncio documentation | About 61,000 characters retained; cancellation passage selected. |
| MDN CSS grid documentation | About 11,000 characters via HTTP; readable introduction. |
| MDN forced Chromium render | About 177,000 characters retained, including navigation; query selects the main heading. |
| HTTPX GitHub repository | Readable project description and documentation. |
| Python Wikipedia article | About 173,000 characters retained. |
| Hacker News index and BBC News | Readable index/news text. |
| NASA RSS, Cloudflare `llms.txt`, PyPI JSON | Native text/structured content recovered. |
| W3C sample PDF | Expected text recovered with page provenance. |
| ScrapingCourse anti-bot challenge | HTTP 403; Chromium also blocked; explicitly reported as suspected error. |
| Reddit Python community | HTTP 403; Chromium also blocked; diagnostic body preserved. |

These checks establish retrieval and selected-text recovery for those samples, not completeness,
accuracy, stable future availability or a web-wide success rate. The isolated container was not
rebuilt or assessed as part of these public smoke checks.

## Remaining coverage limits

`page_status=ok` means no known error signal was detected. Main-content heuristics can still omit
comments, peripheral tables, code or unusual layouts. Content loaded late, behind a login or
paywall, in an iframe, or through an unsupported interaction may be absent. A generic renderer
cannot infer when every application has finished loading. Scanned documents depend on OCR quality
and available languages. Audio/video transcription, Office documents and EPUB are not supported.

The isolated runtime has a deliberate coverage tradeoff. The native broker allows GET, HEAD and a
bounded POST body, forwards `Cookie`, `Referer` and the standard client hints within size caps, and
strips `Authorization` and any header outside its list. A rendered page may POST only to its own
origin; cross-origin POST, other write methods, service workers, WebSockets and downloads are
blocked. So a challenge round trip that needs a cookie works, while a flow that needs a credential
does not. Browser stealth cannot repair a missing protocol step, and the broker supplies its own
TLS/HTTP identity rather than Chromium's.

Cloudflare documents [automated browsers as unsupported for solving production challenges](https://developers.cloudflare.com/cloudflare-challenges/reference/supported-browsers/).
It also provides the reliable [`cf-mitigated: challenge` response signal](https://developers.cloudflare.com/cloudflare-challenges/challenge-types/challenge-pages/detect-response/).
The implementation uses that signal and reports remaining blocks; it does not promise to solve
CAPTCHAs automatically.

The next substantial coverage improvement would be an explicitly enabled local browser mode
with a separate persistent profile and human handoff for login/challenges. It could remain behind
`read_url`, preserving the two-tool interface, but needs a distinct permission/network design:
what the isolated policy still refuses is not cookies but credentials, which never enter the
container. A separate
profile avoids silently using a person's everyday browser credentials. Even that mode would not
guarantee access to every protected site.

For ongoing reliability, expand the representative retrieval corpus and record exact missing
passages and access failures separately. Keep search availability checks distinct: upstream search
engine blocks and cooldowns are already reported by `web_search`, and page extraction cannot make
an unavailable search engine return results.

To use these source changes, restart the development MCP server. For the packaged isolated runtime,
rebuild it through the existing installation/update flow, then reconnect the MCP client.
