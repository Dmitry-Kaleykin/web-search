from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from ..dates import published_at_from_html
from ..models import Document
from ..safety.urls import (
    UnsafeUrlError,
    canonicalize_url,
    is_proxy_fake_ip,
    resolve_redirect,
    validate_public_url,
)
from ..storage import SQLiteStore
from ..text import extract_html_fallback


class ReaderError(RuntimeError):
    pass


class UnsupportedContentError(ReaderError):
    pass


class HTTPReader:
    def __init__(
        self,
        *,
        store: SQLiteStore | None = None,
        cache_ttl_seconds: int = 21_600,
        timeout_seconds: float = 25.0,
        user_agent: str = "LocalResearchBot/0.1",
        max_response_bytes: int = 5_000_000,
        allow_private_urls: bool = False,
        allow_proxy_fake_ips: bool = False,
        max_redirects: int = 5,
    ) -> None:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("httpx is required; install the project dependencies") from exc
        self.store = store
        self.cache_ttl_seconds = cache_ttl_seconds
        self.max_response_bytes = max_response_bytes
        self.allow_private_urls = allow_private_urls
        self.allow_proxy_fake_ips = allow_proxy_fake_ips
        self.max_redirects = max_redirects
        self._client = httpx.AsyncClient(
            timeout=timeout_seconds,
            follow_redirects=False,
            headers={
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.1",
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def read(self, url: str) -> Document:
        canonical = canonicalize_url(url)
        if self.store:
            cached = self.store.get_document(canonical, self.cache_ttl_seconds)
            if cached is not None:
                cached.warnings = [*cached.warnings, "cache_hit"]
                return cached

        current = canonical
        response = None
        for _ in range(self.max_redirects + 1):
            try:
                validated = await validate_public_url(
                    current,
                    allow_private=self.allow_private_urls,
                    allow_proxy_fake_ips=self.allow_proxy_fake_ips,
                )
            except UnsafeUrlError as exc:
                raise ReaderError(str(exc)) from exc
            try:
                response = await self._bounded_get(
                    current,
                    proxy_fake_dns=any(
                        is_proxy_fake_ip(ipaddress.ip_address(value))
                        for value in validated.addresses
                    ),
                )
            except Exception as exc:
                if isinstance(exc, ReaderError):
                    raise
                raise ReaderError(f"Fetch failed for {current}: {exc}") from exc
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    raise ReaderError("Redirect response has no Location header")
                current = resolve_redirect(current, location)
                continue
            break
        else:  # pragma: no cover - loop always exits through range exhaustion branch
            raise ReaderError("Too many redirects")

        if response is None:
            raise ReaderError("No response received")
        if response.status_code in {301, 302, 303, 307, 308}:
            raise ReaderError("Too many redirects")
        try:
            response.raise_for_status()
        except Exception as exc:
            raise ReaderError(f"HTTP {response.status_code} for {current}") from exc

        content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        if content_type == "application/pdf" or current.lower().endswith(".pdf"):
            raise UnsupportedContentError(
                "PDF support is planned but not enabled in this milestone"
            )
        if content_type and not (
            content_type.startswith("text/")
            or content_type in {"application/xhtml+xml", "application/xml"}
        ):
            raise UnsupportedContentError(f"Unsupported content type: {content_type}")

        html_text = response.content.decode(response.encoding or "utf-8", errors="replace")
        fallback_title, fallback_content, links = extract_html_fallback(html_text, current)
        published_at, published_at_source = published_at_from_html(html_text)
        content, method, warnings = _extract_main_content(html_text, current, fallback_content)
        title = fallback_title or _title_from_url(current)
        if len(content) < 200:
            warnings.append("possibly_incomplete: extracted content is very short")

        document = Document(
            url=canonical,
            final_url=canonicalize_url(current),
            title=title,
            content=content,
            method=method,
            published_at=published_at,
            published_at_source=published_at_source,
            content_type=content_type or "text/html",
            warnings=warnings,
            links=links[:500],
        )
        if self.store:
            self.store.put_document(canonical, document)
        return document

    async def _bounded_get(self, url: str, *, proxy_fake_dns: bool = False):
        async with self._client.stream("GET", url) as response:
            self._validate_connected_peer(response, proxy_fake_dns=proxy_fake_dns)
            length = response.headers.get("content-length")
            if length and int(length) > self.max_response_bytes:
                raise ReaderError("Response exceeds configured byte limit")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > self.max_response_bytes:
                    raise ReaderError("Response exceeds configured byte limit")
            decoded_headers = {
                key: value
                for key, value in response.headers.items()
                if key.lower() not in {"content-encoding", "content-length"}
            }
            return type(response)(
                status_code=response.status_code,
                headers=decoded_headers,
                content=bytes(body),
                request=response.request,
                extensions=response.extensions,
            )

    def _validate_connected_peer(self, response, *, proxy_fake_dns: bool = False) -> None:
        """Close the DNS-rebinding gap when the transport exposes the peer address."""
        if self.allow_private_urls:
            return
        stream = response.extensions.get("network_stream")
        if stream is None:
            return
        peer = stream.get_extra_info("server_addr")
        if not peer:
            return
        raw_address = peer[0] if isinstance(peer, tuple) else peer
        try:
            address = ipaddress.ip_address(str(raw_address))
        except ValueError:
            return
        proxy_path = (
            self.allow_proxy_fake_ips
            and proxy_fake_dns
            and (is_proxy_fake_ip(address) or address.is_loopback)
        )
        if not address.is_global and not proxy_path:
            raise ReaderError(f"Connected peer is not public: {address}")


def _extract_main_content(
    html_text: str, url: str, fallback_content: str
) -> tuple[str, str, list[str]]:
    warnings: list[str] = []
    try:
        import trafilatura

        extracted = trafilatura.extract(
            html_text,
            url=url,
            output_format="markdown",
            include_links=True,
            include_tables=True,
            include_comments=False,
            favor_recall=True,
        )
    except ImportError:
        extracted = None
        warnings.append("trafilatura_unavailable: used basic HTML extraction")
    except Exception as exc:  # extraction failure should fall back, not lose the page
        extracted = None
        warnings.append(f"trafilatura_failed: {type(exc).__name__}")
    if extracted and len(extracted.strip()) >= 100:
        return extracted.strip(), "http+trafilatura", warnings
    warnings.append("main_extraction_fallback")
    return fallback_content, "http+basic_html", warnings


def _title_from_url(url: str) -> str:
    parsed = urlsplit(url)
    return parsed.path.rstrip("/").rsplit("/", 1)[-1] or parsed.hostname or url
