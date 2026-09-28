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

## What this means

**The rendered path already looks like desktop Chrome on the wire.** GREASE in the ClientHello,
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

## Re-measuring

```bash
.venv/bin/python tools/transport_fingerprint.py compare   # HTTP reader vs browser reader
.venv/bin/python tools/transport_fingerprint.py chromium  # browser reader only, full capture
```

The probe binds `127.0.0.1` on an ephemeral port and uses a throwaway self-signed certificate in a
temporary directory. It fetches only its own server; nothing leaves the machine.

When the browser mode prints `Blocked by anti-bot protection: Near-empty content`, that is the
probe's own minimal hand-served HTTP/2 response confusing Crawl4AI's content heuristic, not a site
refusing the reader. The ClientHello and frame sequence, which is what the probe measures, are
already captured by then.
