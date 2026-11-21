# Transport fingerprints (Layer 3)

Detection stacks grade headless automation in layers. Layers 1 and 2 (browser API state,
rendering and GPU output) are covered in [README.md](../README.md#rendered-page-identity). This
note records what is and is not reachable at the transport layer, with measurements.

## Measured

Produced by [`tools/transport_fingerprint.py`](../tools/transport_fingerprint.py), a local TLS server
that parses the ClientHello from raw bytes before completing the handshake and logs the HTTP/2
frame sequence as it arrives. Same conditions for every client, so compare columns with each other
rather than treating the numbers as absolute: the target is an address literal, so there is no SNI
and no real ECH negotiation.

| | HTTP reader before | HTTP reader now | Browser reader |
|---|---|---|---|
| JA3 | `9ba9feb4b78a11686815b59cfb700a40` | same | `baff8a6bd5f70dee5fdd88f32e2d76e4` |
| GREASE | none | none | 1 cipher, extensions `0x2a2a`, `0xcaca` |
| Cipher / extension count | 17 / 11 | 17 / 11 | 16 / 17 |
| ALPN | `http/1.1` | `http/1.1, h2` → **h2 negotiated** | `h2, http/1.1` |
| ECH, application_settings | no, no | no, no | yes, yes |
| signature_algorithms count | 26 | 26 | 11 |
| HTTP/2 SETTINGS | n/a (HTTP/1.1) | 6 params, `INITIAL_WINDOW_SIZE=65535`, `MAX_CONCURRENT_STREAMS=100` | 4 params, `HEADER_TABLE_SIZE=65536`, `INITIAL_WINDOW_SIZE=6291456`, `MAX_HEADER_LIST_SIZE=262144` |
| HTTP/2 frames | n/a | SETTINGS, WINDOW_UPDATE(16777216), HEADERS, WINDOW_UPDATE(16777216) | SETTINGS, WINDOW_UPDATE(15663105), HEADERS+priority(`dep=0,exclusive,w=256`) |
| Request header order | n/a | `accept, accept-encoding, user-agent` | `pragma, cache-control, sec-ch-ua, sec-ch-ua-mobile, sec-ch-ua-platform, upgrade-insecure-requests, user-agent, accept-language, accept, sec-fetch-site, sec-fetch-mode, sec-fetch-user, sec-fetch-dest, accept-encoding, priority` |

## What the isolated deployment actually presents

The table above was measured on the host, where the browser reader lets Chromium speak for itself.
In the isolated runtime there is no such thing as "Chromium's bytes": the container runs
`--network=none`, every subresource is intercepted and re-issued by the caller, and the only process
holding a public socket is the native broker. Search and rendering therefore share one transport
identity - the broker's - and it is Apple's `libcurl`/SecureTransport, not a browser's.
[`tools/broker_identity.py`](../tools/broker_identity.py) measures that identity by driving the
installed `build/Web Search Network.app/Contents/MacOS/WebSearchNetwork --test-transport` binary
against public fingerprinting endpoints, because the broker refuses loopback probes by policy. The
broker's TLS identity belongs to the deployment, so "the browser route looks like Chrome" should
never be quoted from the host table.

Measured on macOS 27.0.1 (Apple Silicon) on 2026-11-21; consecutive runs produced identical output:

| | broker |
|---|---|
| JA3 | `375c6162a492dfbf2795909110ce8424` |
| JA4 | `t13d497h2_0d8feac7bc37_7395dae3b2f3` |
| GREASE | none |
| Ciphers / extensions | 49 / 7 (`supported_versions`, `key_share`, `server_name`, `ec_point_formats`, `supported_groups`, `signature_algorithms`, `alpn`) |
| Encrypted Client Hello | no, `server_name` in plaintext |
| ALPN / negotiated | `h2`, `http/1.1` → **h2** |
| HTTP/2 SETTINGS | `MAX_CONCURRENT_STREAMS=100`, `INITIAL_WINDOW_SIZE=10485760`, `ENABLE_PUSH=0` |
| HTTP/2 WINDOW_UPDATE | `1048510465` |
| Pseudo-header order | `:method, :scheme, :authority, :path` |
| Request header order | `accept-encoding, user-agent, accept` - the emission order, not the caller's |
| Akamai h2 fingerprint | `3:100;4:10485760;2:0\|1048510465\|0\|m,s,a,p` |

For contrast, the same endpoint reports Chrome as `1:65536;2:0;4:6291456;6:262144|15663105|0|m,a,s,p`.
So the broker is a clean, stable, recognisably non-Chrome client: no GREASE, no ECH, a SecureTransport
ClientHello and nghttp2 defaults. That is consistent and explicable, which is the useful property;
the alternative - a different identity per request - is worse. One instance of that was real: header
order used to come out of Swift's dictionary iteration, so the same request could reach the server
with two different header orders. Emission now follows a fixed list (`docs/isolation.md`), and the
tool's `header_order` field confirms it: repeated runs report the same order, in which the injected
`accept-encoding` appears where libcurl puts it and the caller's headers follow the policy list.

Read these numbers with the same care as the host table. They were taken from one network through
one observer, so IP-level facts (TTL, window size, MSS, TCP option order) describe the path as much
as the client, and the TLS identity belongs to the operating system: a macOS update can move the
JA3/JA4 hashes with no change in this repository. Unlike the local probe, these requests carry a
real `server_name`, which is why the JA3 differs from the host column in kind as well as value.

## What this means

**On the host, the rendered path already looks like desktop Chrome on the wire.** GREASE in the ClientHello,
encrypted ClientHello, `application_settings`, Chrome's SETTINGS values, its
`WINDOW_UPDATE 15663105`, its HEADERS priority and its header order are all present.
`chrome-headless-shell` and the full "new headless" build share one network service, so selecting
`WEB_SEARCH_BROWSER_CHANNEL=chromium` does **not** change the TLS or HTTP/2 fingerprint; its value
is Layer 2 (a real GPU and WebGL path), and it stays opt-in. The full build could not be launched in
this sandbox at all, so its rendering behaviour is unverified here — what *was* observed is the
fail-safe: the reader logs `Chromium build 'chromium' failed to start; retrying with default`, falls
back, and the read still succeeds with the fingerprint above.

**The plain HTTP path has a non-browser TLS fingerprint and that is not fixable in-process.** It is
OpenSSL: no GREASE, no encrypted ClientHello, 26 signature algorithms. Python's TLS stack cannot
gain GREASE or ECH, so its JA3 stays distinct from Chrome's whatever else is tuned; emulating Chrome's
ClientHello needs a separate shim such as curl-impersonate or tls-client. What was changed is the
transport: HTTP/2 with `h2` in ALPN, which the measurement confirms now negotiates.

Partial mimicry was rejected on purpose. Forcing Chrome's cipher order onto a ClientHello that still
lacks GREASE creates precisely the kind of layer disagreement the detection hierarchy grades, and a
path that identifies itself as `LocalResearchBot` gains nothing by half-impersonating a browser.

**Network origin is the one transport property that can still be moved.** `WEB_SEARCH_BROWSER_PROXY`
routes rendered fetches through an HTTP or SOCKS proxy, which changes the address the site sees. Note
that the proxy, not this process, then resolves names for the browser, so `validate_public_url`'s
local resolution no longer describes the browser's actual peer: keep it pointed at a proxy you chose.
The isolated deployment refuses proxies altogether, so there the address the site sees is this
machine's, and the names it sees are the broker's two DNS-over-HTTPS resolvers'.

## Re-measuring

```bash
.venv/bin/python tools/transport_fingerprint.py compare   # HTTP reader vs browser reader
.venv/bin/python tools/transport_fingerprint.py chromium  # browser reader only, full capture
.venv/bin/python tools/broker_identity.py                 # installed isolated broker, public probes
```

`transport_fingerprint.py` binds `127.0.0.1` on an ephemeral port and uses a throwaway self-signed
certificate in a temporary directory; it fetches only its own server, so nothing leaves the machine.
`broker_identity.py` is the opposite kind of probe and needs the network: it sends a few requests
from the installed broker to public fingerprinting services, which see this machine's address and
record the request.

When the browser mode prints `Blocked by anti-bot protection: Near-empty content`, that is the
probe's own minimal hand-served HTTP/2 response confusing Crawl4AI's content heuristic, not a site
refusing the reader. The ClientHello and frame sequence, which is what the probe measures, are
already captured by then.
