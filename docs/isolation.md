# Isolated web search on macOS

The `web-search-isolated` launcher runs the complete MCP server, SearXNG, Chromium,
PDF/image extraction and OCR in a nonroot, read-only Docker container with
`--network=none`. There are no published ports, host directory mounts, Docker socket,
host home-directory access, inherited API credentials, or project `.env` in that container.
Only its own named Docker data volume and bounded temporary files are writable.

A native macOS executable in `build/Web Search Network.app` owns external connections.
Python's page reader, Chromium requests, Crawl4AI's robots reader, and SearXNG engine
HTTP clients request retrieval through a local Unix socket. The worker supervisor
multiplexes those requests over Docker's standard input/output. The broker starts one
fixed container command; it is not a general proxy, has no listening port, and cannot
be asked to execute commands or read a file.

The broker applies macOS filesystem restrictions after starting Docker. It cannot
read user files or start further processes. Its environment is cleared so inherited
proxies, alternate certificate stores, and credentials cannot change networking.
The Docker CLI uses a pinned local Unix socket and an empty dedicated configuration,
without inherited remote contexts, proxies or registry credentials.

## Install and connect Pi

Start Docker Desktop, then run from the project directory:

```sh
.venv/bin/python tools/build_isolated.py
.venv/bin/python tools/check_isolation.py
```

The build needs Apple's Command Line Tools (`xcode-select --install`) and downloads
public dependencies and browser binaries. Build-time traffic belongs to Docker;
runtime page and search traffic belongs to Web Search Network. The Python base image,
SearXNG source image, and runtime dependencies are pinned. The launcher records
the built image's immutable ID and uses `--pull=never`; it never silently runs a changed
tag or falls back to the unrestricted server.

Use `integrations/pi/mcp-server.example.json`, whose command is the absolute path to
`web-search-isolated`. No environment settings, model credentials or sampling approvals
are needed. Restart Pi after changing its MCP configuration. The project `.env` and
legacy Pi environment settings do not configure the isolated worker. Change its image
configuration deliberately and rebuild when needed.

In Little Snitch, create a rule for this executable, with process identity checking:

```text
build/Web Search Network.app/Contents/MacOS/WebSearchNetwork
```

Allow outgoing TCP ports 80 and 443 for this executable. Do not create a general
Terminal, Python, Chromium, or Docker internet allowance for runtime search.
Little Snitch may display a process pair such as Terminal via WebSearchNetwork;
select the specific executable rather than the parent application. The app uses a
local ad-hoc signature, not an Apple Developer ID. Rebuilding changes the code and
may require confirming the new identity in Little Snitch. Do not disable identity
checks to avoid this. Its stable path, bundle identifier, and display name remain.
The build also creates `build/Web Search Network.lsrules` for import into Little Snitch.

The old standalone SearXNG container is not used by this launcher. Existing host caches
are not mounted or deleted. The isolated cache lives in the Docker volume
`local-web-search-isolated-data`. Each MCP connection starts and stops its own worker.

## Terminal console

Run `./web-search` and use **Start services** as usual. It starts Docker if necessary,
checks the installed image and native executable, and stops the obsolete standalone
`local-web-search-searxng` container. It leaves any connected Pi worker running.
The status lists Pi's worker containers by name; zero workers is normal when Pi is
disconnected. Reopen an already running console after updating its code.

Setup builds the complete isolated image, including search and Chromium. Readiness
checks use `tools/check_isolation.py` and its temporary isolated worker. Live logs follow
the broker audit file. Stop search workers affects only this project's Pi workers;
Docker and unrelated containers stay running. Reconnect Pi after stopping its worker.
There is no standalone SearXNG restart action, since Pi owns worker connections.

## Enforced network policy

- Public HTTP or HTTPS only, using the corresponding standard port 80 or 443.
- GET and HEAD only, with no bodies, uploads, CONNECT tunnels or URL credentials.
- Only bounded Accept, Accept-Language, User-Agent, Range and conditional-cache headers
  are forwarded. Authorization, Cookie, Referer and arbitrary headers are discarded.
- TLS certificate and hostname verification remain enabled. No interception CA is installed.
- A 5 MB decoded response limit, 64 KB header limit, 4 KB URL limit, 45-second transfer
  timeout, and bounded concurrent/queued requests.
- Each connection's actual IP is checked before opening its socket. Loopback, LAN,
  link-local, cloud metadata, synthetic proxy, reserved and special-use ranges are blocked.
  IPv6 is restricted to global unicast, excluding transition and documentation ranges.
- Redirects return to the caller. Subsequent requests go through the same policy;
  the page reader limits redirect chains.
- Browser requests are fulfilled from broker responses. Service workers, WebSockets,
  downloads and write requests are blocked. The container's network namespace also
  prevents direct networking, including WebRTC and QUIC.

The broker uses certificate-verified DNS over HTTPS at the fixed public address
`https://1.1.1.1/dns-query`, avoiding local/TUN synthetic DNS without permitting private
connections. Cloudflare receives queried hostnames. libcurl generates the fixed DNS
protocol requests internally; this is not an API for submitting arbitrary POST data.
The target website sees the broker's TLS/HTTP fingerprint rather than Chromium's.

Strict method and cookie restrictions mean some search engines, login walls, browser
challenges and interactive pages will fail. There is no automatic security exception.
The caller sees retrieval failures through the existing error/warning fields.

## Logs and verification

`.web-search-data/isolation/network.jsonl` records time, target hostname, method, status,
decoded response size and policy/transport errors. It omits paths, query strings,
headers, search text and page contents. The log rotates between launches at 5 MB.
The regular isolated application cache still contains fetched public content and
submitted queries. Avoid using confidential text as a search query.

`tools/check_isolation.py` verifies the container's network namespace, blocked direct
connections, absence of host files and Docker socket, readonly filesystem, MCP handshake,
public HTTPS reading, private-IP rejection, real Chromium rendering/screenshots and
a real SearXNG search. Tests in `tests/isolation` exercise blocked methods/bodies,
binary RPC, browser routing and failure behavior. The native executable's `--self-test`
checks its own address, scheme, credentials, method and body policies.

## Security boundary and limitations

This isolates the web-search tool, not Pi's shell tools, your whole account or the
Docker daemon. An administrator, a compromised Docker daemon, an OS/container escape,
or someone able to replace the broker/build can invalidate the boundary. Little Snitch
identity checks help detect executable replacement. Do not give Pi unrestricted network
access through another tool if that contradicts your intended policy.

The worker cannot read your home files, but Pi can supply text it already knows in a
search query, URL, hostname, or permitted header. Reading an arbitrary public website
necessarily sends a request to it. GET requests can disclose data or trigger changes on
poorly designed websites. No read-only-method filter can promise that arbitrary searches
and URLs cannot convey information. This implementation prevents file access and ordinary
upload/write channels; it does not claim complete prevention of information disclosure.

macOS's `sandbox_init` is deprecated and may be removed in a future OS. Startup fails if
the broker cannot apply its restrictions; it never retries without them. Updates to the
pinned Crawl4AI or SearXNG versions require reviewing their client/context integration
and rerunning the full isolation checks.
