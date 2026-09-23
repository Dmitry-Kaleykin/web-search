# Search availability

The server still exposes only `web_search` and `read_url`. It does not call a model or require paid
search APIs. Access health is separate from result relevance and research completion.

## What a search does

A valid cached result remains usable while engines are blocked. Its original retrieval time and
warnings remain unchanged. Reading the cache does not establish engine recovery.

For fresh discovery, the server excludes engines on cooldown or already being tested for recovery.
It makes at most one SearXNG search request. Simultaneous identical calls within one MCP server share
that request, including `refresh=true`. Different queries still use the bounded, serialized queue.
Cancelling one waiter leaves other waiters running; cancelling the last stops the shared operation.

The server also reads the local backend's `/config` inventory, cached for 15 minutes. It filters
missing/disabled engines and engines that do not support the requested pagination or time filter.
It never silently removes a requested filter. If none can execute the request, diagnostics say so.
An unavailable inventory is retried after one minute; discovery can continue with configuration
explicitly marked unverified. These metadata reads do not query external search engines.
The doctor forces a fresh inventory check. Enabled engines outside the operator's configured pool
are not automatically admitted: being enabled does not establish useful coverage.

## Health and recovery

`engine_status` is an additional structured output field. It reports each configured engine and the
SearXNG endpoint, including status, consecutive failures, repeated-failure flag, success/failure
counts, last observed success/failure, last successful execution latency, and estimated next retry.
Timestamps are Unix seconds. Missing observations are null, not inferred successes.

- `untested`: no verified execution recorded.
- `working`: verified execution observed in the last day; not a promise about the next request.
- `unverified`: the last success is older than a day.
- `cooling_down`: a failed or inconclusive attempt requires waiting.
- `recovery_due`: the wait expired, but recovery has not been verified.
- `recovering`: another request holds the engine's recovery lease.
- `disabled` / `missing`: excluded by the backend inventory.

Verification uses attributed results or SearXNG's per-engine `Server-Timing` entries. A successfully
executed empty search is healthy. An empty aggregate response without execution evidence proves
neither success nor failure for an individual engine. Unsupported filters do not count as failures.
Latency is unavailable when the backend supplies attribution but no engine timing.

Initial waits match the bundled SearXNG suspension policy: one hour for rate limiting, one day for
access denial/CAPTCHA, and the longer documented waits for reCAPTCHA and Cloudflare CAPTCHA.
Transport failures start with a shorter wait. Consecutive failures increase the delay, with a bounded
local backoff; an explicit `Retry-After` is never shortened, including HTTP-date headers. Suspension
reports are recorded without counting them as new failed upstream attempts.

SearXNG's JSON response does not expose the exact remaining engine suspension time. Consequently,
retry timestamps are estimates. Remote instances may have different policies. An expired wait
allows a guarded probe; it never establishes recovery by itself.

At most one recovering engine is included in each search. A SQLite transaction grants an exclusive,
expiring recovery lease across MCP/doctor processes sharing the data directory. Crashes cannot leave
permanent leases. Failure extends the wait; verified success restores normal use. Inconclusive or
cancelled probes release the lease and wait before another attempt, without inventing failures.
A late response cannot erase a newer recorded failure. There is no background engine-probing loop.

The existing SQLite health table is upgraded without deleting cooldowns or historical journals.
Recent health records survive expiry and maintenance; records unused for 90 days may be pruned.
Use a separate data directory for a different SearXNG backend, as caches and engine history belong
to the configured instance.

## Backend versions and reserve engines

Docker Compose pins the image digest of the tested local SearXNG version. An ordinary restart or
pull therefore cannot silently switch versions. To update, test a candidate image on a separate
local port, run the retrieval baseline against it, inspect source quality, and only then replace the
pinned digest. Keep the preceding digest for rollback. The container has a local readiness check;
the console waits for readiness on startup so initial searches do not race backend initialization.
This check contacts only the local configuration endpoint, not external engines.

On 2026-09-23, isolated Bing and Wiby searches were tested with English and Russian queries about
Python asyncio TaskGroup cancellation. Bing returned broad Python home/download/tutorial links,
with the same first five hits for both queries. Wiby executed successfully but returned no hits.
Neither was added to the default pool based on these results. This is a narrow coverage test, not a
claim that either engine is universally useless. Additional reserve candidates need comparable
language/topic checks before activation; successful HTTP responses alone are insufficient.

The regression suite covers schema migration, retained history, increasing waits, suspension echoes,
exclusive recovery leases, empty responses, unsupported filters, cache provenance, HTTP-date retry
headers, stale responses, cancellation, and simultaneous refresh calls. Use `web-search-doctor` for
current live access and the retrieval baseline for inspection of actual results.

## Validation on 2026-09-23

152 local tests passed, plus both optional Chromium integration tests and the Pi workflow test.
The built wheel passed offline MCP checks from outside the checkout. Live readiness and all four
retrieval-baseline cases passed. A startup disconnect was observed and addressed with the container
readiness check; the persisted endpoint cooldown expired normally and recovery then succeeded.
Searchmysite and Mojeek were each retried on separate subsequent searches, reported access denial,
and returned to cooldown. Other engines continued supplying results. These checks demonstrate
retrieval and recovery behavior, not general answer accuracy.
