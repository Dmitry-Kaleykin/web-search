# Isolation coverage and robustness work plan

Findings that motivated this plan are measured, not assumed. Reproduce them with
`tools/broker_identity.py`, `tools/check_isolation.py`, and the `engine_status` block of
`web_search(diagnostics=true)`.

## Measured starting point (2026-11-21)

| Evidence | Result |
| --- | --- |
| Broker wire identity (`httpbun.com/get` via `--test-transport`) | only `Accept`, `Accept-Encoding: deflate, gzip`, `Accept-Language`, `Host`, `User-Agent` reach the origin |
| Same request | `Cookie`, `Referer`, `Authorization`, `Sec-*`, `Content-Type` discarded, silently |
| Same request (`cloudflare.com/cdn-cgi/trace`) | `http/2`, `TLSv1.3`, `sni=plaintext`, Apple SecureTransport/libcurl identity |
| `web_search` live | `requested: 6 engines`, `available: 3`, `outcome: success` |
| `engine_health` in the data volume | `duckduckgo web` 0 successes / 10 failures (cooling 21 h), `searchmysite` 0/3 (cooling 4 d), `mojeek` access denied (cooling 24 h) |
| `links.duckduckgo.com/d.js` through the broker | HTTP 202 with a JavaScript challenge, not JSON → SearXNG "parsing error" |
| `www.mojeek.com/search` through the broker | HTTP 200 whose body is a CAPTCHA interstitial |
| `searchmysite.net/search` through the broker | HTTP 403 Cloudflare "Attention Required!" |
| Broker audit log, 663 requests | 79× 403, 14× SSL connect error, 13× timeout, 4× DNS failure, every request a GET |

## Policy decision

The isolation requirement is a *network identity* requirement: one Little Snitch-allowable
executable owns all public egress, and nothing else on the machine needs an allowance. The
method/header/cookie restrictions were added on top of that identity to make the broker
fail-closed. Measured against the actual container, they cost half the search fleet and most
challenge-based pages, so they are kept only where they still buy something:

Kept, because it protects the host:

- Public HTTP(S) destinations only, standard ports, no URL credentials, actual-peer checks
  before `connect()`, certificate-verified DNS over HTTPS, no proxies, cleared environment.
- The broker reads no user files, starts no processes, and accepts no request bodies beyond a
  bounded cap. The container keeps `--network=none`.
- Write methods other than `POST` stay blocked. WebSockets, service workers and downloads stay
  blocked: they need real transport access, not a policy exception.
- `Authorization` and other credential headers stay out of the container. Credentials for
  API-backed providers belong to the broker, in a 0600 file it reads before sandboxing.

Relaxed, because the risk is bounded and the container holds nothing worth stealing:

- `Cookie` requests and `Set-Cookie` responses are allowed, bounded in count and size. The jar
  lives in the caller (httpx, Chromium), never in the broker, and holds only cookies obtained
  during the session from public origins.
- `Referer` and the `Sec-Fetch-*` / `Sec-CH-UA*` / `Priority` families are forwarded. Two
  enabled SearXNG engines set them specifically to avoid blocks, and a browser route that hides
  them presents a Chrome identity that its own request headers contradict.
- Bodyless `GET`/`HEAD` gains a bounded `POST` (`application/x-www-form-urlencoded`,
  `application/json`, `text/plain`, ≤ 64 KB). `GET` query strings already give a public origin
  the same reporting bandwidth; a body cap keeps that true.
- In the browser route, `POST` is additionally limited to the frame's own origin, so a rendered
  page can talk to its own API but not exfiltrate to an arbitrary host.

## Work items

Tier 1 - see what the boundary is doing (all shipped):

1. Report which request headers and response headers the policy discarded, per fetch and in the
   audit log (names only, never values).
2. Give size, cancellation and DNS failures distinct, stable error classifications.
3. Stop cooldowns treating a locally caused rejection as an upstream suspension.
4. Make `tools/check_isolation.py` report per-engine coverage and fail a configured engine that
   has never answered.
5. Add `tools/broker_identity.py` and correct the transport-fingerprint documentation, which
   measures the host Python path only.

Tier 2 - stop self-inflicted failures (all shipped):

6. Browser sub-resource fetches currently use httpx's 5-second default while the broker budgets
   45 seconds; align them and reuse one client per browser context.
7. Propagate cancellation from the worker to the broker, and queue on backpressure instead of
   returning "capacity exceeded" the instant the queue is full.
8. Classify "response exceeds the byte limit" at the native boundary and stop re-downloading an
   oversized object through Chromium afterwards.
9. Reject one malformed frame instead of exiting the broker with every in-flight request.
10. Add a second DNS-over-HTTPS resolver so name resolution is not a single dependency.

Tier 3 - coverage:

11. Apply the relaxed policy above in `native/`, `isolation/transport.py` and the browser guard.
12. Replace engines that need a JS execution surface (`duckduckgo web` now returns a JS
    challenge; `mojeek` a CAPTCHA) with independent answerers, key-backed ones through broker
    operations so keys never enter the container. Superseded for two of three engines - see
    below - still open for `searchmysite`.

## Outcome

After the policy landed and the recorded cooldowns were cleared, the same six-engine fleet on the
same machine and network:

| Engine | Before | After |
| --- | --- | --- |
| google cse | answering | answering |
| duckduckgo web | 0 successes / 10 failures, cooling 21 h | answering, 0 failures; some queries honestly empty |
| mwmbl | answering | answering |
| mojeek | access denied, cooling 24 h | answering, 7 successes / 1 later refusal |
| crowdview | answering | answering |
| searchmysite | access denied, cooling 4 d | still refused (Cloudflare challenge), 1 probe/day |

`duckduckgo web` and `mojeek` were never engines that needed JavaScript or a solved CAPTCHA. They
needed the cookie their own challenge response set, which the previous policy threw away. They are
not reliable now in the strong sense: mojeek still returns 403 often enough to spend time on cooldown
between successes, and `duckduckgo web` answers some queries with nothing. The difference is that
both now succeed most of the time and fail as reported upstream refusals, instead of failing every
time with a parse error that looked like a bug in this project. The one engine still refusing
altogether is `searchmysite`, behind a Cloudflare challenge that needs a browser the broker is not.

Two defects worth recording, both found by the tests written for the new behaviour rather than by
the earlier probes:

- The isolated worker started watching for a caller hangup *before* reading the request line, and
  that watch consumed the line's first byte. Every isolated fetch then failed as
  `Extra data: line 1 column 6` - a framing error reported as "fetch failed for https://example.com".
  It survived until a real socket round trip existed in the test suite; per-side mocks could not
  see it, because both ends were wrong in complementary ways.
- Request header order came out of Swift dictionary iteration, so identical requests reached the
  same origin with different header orders. Emission now follows a fixed order.

Still open:

- `searchmysite`: choose a replacement or accept one Cloudflare refusal a day. Deciding needs an
  engine that is genuinely independent of the others, not another HTML scraper.
- `mojeek`: intermittent 403s. It earns its place only if its successes keep coming; if it spends
  most of each day on cooldown it is noise that the coverage report now makes visible anyway.
- Engine cooldowns honour "never shorten recorded evidence", so a wait written by an older policy
  outlives the policy that caused it. A fingerprinted backend change now clears the evidence
  automatically (`engine_health_reset:` warning); an install upgrading from an unfingerprinted
  store needed one manual clear.
- The rebuilt broker needs its Little Snitch allowance re-approved before its wire identity can be
  re-measured (`tools/broker_identity.py`).
