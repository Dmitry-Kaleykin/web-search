from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit


class UnsafeUrlError(ValueError):
    """Raised when a URL is not safe for the public web reader."""


TRACKING_PARAMETERS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "ref_src",
    "source",
}

# RFC 2544 benchmarking space is widely used by local TUN proxies for synthetic
# DNS answers. It is not treated as public by ipaddress, so accepting it must be
# explicit and is limited to hostname resolutions (never literal-IP URLs).
PROXY_FAKE_IP_NETWORKS = (ipaddress.ip_network("198.18.0.0/15"),)


@dataclass(frozen=True, slots=True)
class ValidatedUrl:
    url: str
    host: str
    addresses: tuple[str, ...]


async def validate_public_url(
    url: str,
    *,
    allow_private: bool = False,
    allow_proxy_fake_ips: bool = False,
) -> ValidatedUrl:
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise UnsafeUrlError("Only http and https URLs are allowed")
    if not parsed.hostname:
        raise UnsafeUrlError("URL has no hostname")
    if parsed.username or parsed.password:
        raise UnsafeUrlError("Credentials in URLs are not allowed")

    host = parsed.hostname.rstrip(".").lower()
    if (host == "localhost" or host.endswith(".localhost")) and not allow_private:
        raise UnsafeUrlError("Localhost URLs are blocked")

    try:
        direct_ip = ipaddress.ip_address(host)
        addresses = (str(direct_ip),)
        hostname_resolved = False
    except ValueError:
        addresses = await _resolve(host, parsed.port or _default_port(parsed.scheme))
        hostname_resolved = True

    if not addresses:
        raise UnsafeUrlError("Hostname did not resolve")
    if not allow_private:
        for value in addresses:
            address = ipaddress.ip_address(value)
            proxy_fake_ip = allow_proxy_fake_ips and hostname_resolved and is_proxy_fake_ip(address)
            if not address.is_global and not proxy_fake_ip:
                raise UnsafeUrlError(f"Non-public destination is blocked: {address}")

    return ValidatedUrl(url=urlunsplit(parsed), host=host, addresses=addresses)


def is_proxy_fake_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(address in network for network in PROXY_FAKE_IP_NETWORKS)


async def _resolve(host: str, port: int) -> tuple[str, ...]:
    def resolve() -> tuple[str, ...]:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        return tuple(sorted({str(info[4][0]) for info in infos}))

    try:
        return await asyncio.to_thread(resolve)
    except socket.gaierror as exc:
        raise UnsafeUrlError(f"Hostname resolution failed: {host}") from exc


def _default_port(scheme: str) -> int:
    return 443 if scheme.lower() == "https" else 80


def canonicalize_url(url: str) -> str:
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    port = parsed.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{hostname}:{port}"
    else:
        netloc = hostname
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    query = urlencode(
        sorted(
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key.lower() not in TRACKING_PARAMETERS and not key.lower().startswith("utm_")
        )
    )
    return urlunsplit((scheme, netloc, path, query, ""))


def resolve_redirect(base_url: str, location: str) -> str:
    return urljoin(base_url, location)


def registrable_domain(url: str) -> str:
    """Return a public-suffix-aware key for independence/source-family checks."""
    try:
        from tld import get_fld

        value = get_fld(url, fail_silently=True)
        if value:
            return value.lower()
    except ImportError:  # pragma: no cover - tld is a declared runtime dependency
        pass
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host
