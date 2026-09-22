from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit


class UnsafeUrlError(ValueError):
    """Raised when a URL is not safe for the public web reader."""


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
                if hostname_resolved and is_proxy_fake_ip(address):
                    raise UnsafeUrlError(
                        f"Synthetic proxy DNS address is blocked: {address}. "
                        "If this machine uses a trusted TUN/fake-IP proxy, set "
                        "WEB_SEARCH_ALLOW_PROXY_FAKE_IPS=true; keep "
                        "WEB_SEARCH_ALLOW_PRIVATE_URLS=false."
                    )
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
    """Conservative identity key: preserve path, query order/encoding and meaningful parameters."""
    parsed = urlsplit(url)
    if parsed.username or parsed.password:
        raise UnsafeUrlError("Credentials in URLs are not allowed")
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    host = f"[{hostname}]" if ":" in hostname else hostname
    port = parsed.port
    netloc = host
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"
    return urlunsplit((scheme, netloc, parsed.path or "/", parsed.query, ""))


def resolve_redirect(base_url: str, location: str) -> str:
    return urljoin(base_url, location)
