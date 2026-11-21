# What each observer learns about you

Isolation answers one question: which binaries on this Mac are allowed to reach the internet, and
what can the thing that does the reaching see about the machine. It does not answer the anonymity
question - who can tell that *you* read *this* page - and the two get confused often enough to be
worth writing down separately. Everything below was measured on 2026-11-21 on the deployed
macOS runtime, with the commands at the end.

## Who learns what

| Observer | Sees | Does not see |
| --- | --- | --- |
| The site you read | Egress IP, full URL including path and query, your cookies for it, the headers the broker forwards, a TLS/HTTP2 fingerprint that is not Chrome's | Your machine, your other reads, your DNS names beyond its own |
| Your VPN provider (and any on-path network) | The destination IP and the plaintext SNI hostname of every connection, timing and volume | Path and query - they stay inside TLS |
| The DNS-over-HTTPS resolvers (Cloudflare, Quad9) | Every hostname looked up, from the search engines onward | Paths, queries, cookies |
| The search engines | Your verbatim query text, egress IP, the engine list; the engine logs the query | Everything you read afterwards |
| The container | Its own request/response bodies; the shared volume | Cookies and credentials of the host, since those live in the caller |
| Your own disk | Every query, every page body, hostnames and outcomes in the audit log | Nothing - this is the intended record |

Two consequences follow from that table. Sites always learn the URL, because a web server cannot
serve a path it is not told: an anonymity layer that hides paths would have to proxy and rewrite,
which is a different product. And the observer that currently learns the most after the destination
is the one carrying the tunnel, because SNI is still sent in plaintext inside it.

## Egress identity, measured

`tools/egress_identity.py`, run through the installed broker:

```text
31.220.5.13  Stockholm, SE  AS63473 HostHatch LLC
```

HostHatch is a hosting provider, so this is a datacenter exit, not a residential line. The reason is
visible in the routing table: the IPv4 default route is `utun8`, a system VPN tunnel provided by
the "Happ" network extension. So the IP that websites already see is not your home address, and the
broker never notices the difference - it resolves names itself over DNS-over-HTTPS and connects to
the answer, and the tunnel carries that connection like any other.

That has three practical consequences.

- **Nothing has to be rented to stop sites seeing your home IP.** It already does not. A rented VPS
  proxy buys a *private and stable* exit rather than a shared datacenter one - a reputation and
  consistency improvement, not more anonymity - and it is your machine and your bill, so it is a
  weaker claim to "someone else's traffic" than the paid mixed-tenancy exit you already use.
- **A datacenter exit costs coverage, not privacy.** Shared hosting ranges are the cheapest way to
  attract a CAPTCHA, which is consistent with `searchmysite` refusing with 403 while the engines
  that tolerate bot traffic answer normally. Attribution is possible by toggling the VPN and
  re-running the engine report; the difference would be that exit's reputation, not this project's
  headers.
- **The VPN provider is now the on-path observer.** Since the broker's TLS ClientHello carries a
  plaintext SNI, the tunnel's operator sees every hostname. Little Snitch approval for a system VPN
  is a separate allowance for a separate binary by nature; that trade is the user's and predates
  this project.

Tor was considered and is not recommended here: free, but it means installing and keeping a daemon
running (a second allowed binary plus its state directory), its exit ranges are the most
bot-tested addresses on the internet, and an exit node operator with plaintext SNI is the same
trust you already gave the VPN, with worse reputation attached.

## What the code does about it now

- **Cookie lifetime.** The plain HTTP reader's jar is an `ExpiringCookieJar`
  (`src/web_research/safety/cookies.py`): a site's cookie is remembered for
  `WEB_SEARCH_COOKIE_TTL_SECONDS` (default 900) after that site was last heard from, pruned before
  every request. Long enough to finish a paginated or challenge-gated read; short enough that a
  site cannot recognise a returning visitor days later, which matters because an MCP server can run
  for weeks. `0` refuses cookies outright, at the cost of every challenge. The rendered path already
  started each context with an empty jar.
- **Query text stays inside the container.** SearXNG's access log line reported
  `GET /search?q=<your terms>` on stderr, which is forwarded to whoever supervises the MCP process;
  it now reports the route only (`"GET /search" 200 46302`). The `httpx` logger is pinned to
  WARNING, because its INFO line repeats the full URL of every request. Query text still goes where
  it belongs: the cache database you control.
- **Fan-out is visible rather than silent.** A query goes to several engines at once, so several
  engines see it. `engine_coverage` in diagnostics names the engines that were asked and which ones
  answered, which makes the choice to ask fewer of them a decision with a visible cost instead of a
  guess.

## Levers left, with their real costs

| Lever | What it changes | Cost |
| --- | --- | --- |
| VPS or home proxy peer in the broker | A single IP you control, chosen country, stable over months | ~EUR 5/month, your traffic is one tenant on that IP; a broker-side SOCKS5 peer must resolve names itself (`socks5://`, not `socks5h://`), because a name the broker resolves locally would leak over ordinary DNS |
| Tor peer | Free exit diversity | Daemon plus a second allowed binary, worst-reputation exits, no policy gain |
| Encrypted Client Hello | Hides the hostname from the on-path observer, which today means the VPN provider | Apple's libcurl (8.7.1, SecureTransport) has no ECH at all, measured by its absence from `curl --help all`; Homebrew's curl 8.23.0 against OpenSSL 4.0.3 does. Adopting it means relinking the broker against Homebrew libcurl, which changes both the code signature (so a fresh Little Snitch approval) and the TLS fingerprint, plus HTTPS/SVCB record discovery over the existing DoH path to learn the ECH config, plus a fallback when a site has no ECH yet |
| Smaller engine fan-out | Fewer engines see each query | Less coverage, and silent engines become indistinguishable from unasked ones |
| Per-read identity (fresh cookie jar, per-read fingerprint rotation) | A site cannot link consecutive reads | Breaks challenge and pagination flows for the HTTP reader; the rendered path already gets a fresh context per render |

## Deliberate non-goals

No per-request fingerprint rotation, no automatic proxy, no attempt to make this look like Tor. The
product is a personal research tool whose value comes from reading a site that is willing to answer;
an identity that changes on every request is its own strongest detection signal, and the user's
explicit preference is on record for identity coherence over churn.

## Reproduce

```bash
.venv/bin/python tools/broker_identity.py            # wire identity as the broker presents it
.venv/bin/python tools/egress_identity.py            # address and geolocation sites see
.venv/bin/python tools/check_isolation.py            # engine coverage, and no query text in the log
route -n get default                                 # which interface carries the egress
```
