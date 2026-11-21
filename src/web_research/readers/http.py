from __future__ import annotations

import asyncio
import importlib.util
import ipaddress
import json
from urllib.parse import urlsplit

from ..dates import attribution_from_html, published_at_from_html
from ..models import Document
from ..safety.urls import (
    UnsafeUrlError,
    canonicalize_url,
    is_proxy_fake_ip,
    resolve_redirect,
    validate_public_url,
)
from ..storage import SQLiteStore
from ..text import absolute_html_links, extract_html_fallback
from .base import cap_content
from .quality import assess_html_quality, cacheable_document, has_meaningful_text


class ReaderError(RuntimeError):
    pass


class UnsupportedContentError(ReaderError):
    pass


class ResponseTooLargeError(ReaderError):
    """The response is bigger than the transfer budget.

    Rendering cannot help: a browser downloads the same bytes, then adds a full page load. The
    reader raises this so the router can say so instead of spending a render on it.
    """


class HTTPReader:
    def __init__(
        self,
        *,
        store: SQLiteStore | None = None,
        cache_ttl_seconds: int = 21_600,
        timeout_seconds: float = 25.0,
        user_agent: str = "LocalResearchBot/0.1",
        accept_language: str = "en-US,en;q=0.9",
        max_response_bytes: int = 5_000_000,
        max_content_chars: int = 1_000_000,
        allow_private_urls: bool = False,
        allow_proxy_fake_ips: bool = False,
        max_redirects: int = 5,
        cookie_ttl_seconds: float = 900.0,
        document_semaphore: asyncio.Semaphore | None = None,
    ) -> None:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("httpx is required; install the project dependencies") from exc
        self.store = store
        self.cache_ttl_seconds = cache_ttl_seconds
        self.max_response_bytes = max_response_bytes
        self.max_content_chars = max_content_chars
        self.allow_private_urls = allow_private_urls
        self.allow_proxy_fake_ips = allow_proxy_fake_ips
        self.max_redirects = max_redirects
        self.cookie_ttl_seconds = cookie_ttl_seconds
        # Cookies are what let a challenge round trip succeed, and what let a site recognise this
        # reader next week. The jar keeps them for a bounded time and is pruned per request.
        from ..safety.cookies import ExpiringCookieJar

        self.cookie_jar = ExpiringCookieJar(cookie_ttl_seconds)
        self._document_limit = document_semaphore or asyncio.Semaphore(2)
        # h2 is an optional extra of httpx and the base install does not need it. Offering it puts
        # the plain HTTP path on the transport browsers use. The ALPN *order* stays httpcore's
        # (`http/1.1`, `h2`) because it rewrites the context per connection, and GREASE plus
        # encrypted ClientHello are unreachable from Python's TLS stack - so this path keeps an
        # honest non-browser TLS fingerprint rather than a half-finished imitation.
        http2 = importlib.util.find_spec("h2") is not None
        self.http2 = http2
        from ..isolation.transport import BrokerTransport, isolated

        self._client = httpx.AsyncClient(
            timeout=timeout_seconds,
            follow_redirects=False,
            http2=http2,
            cookies=self.cookie_jar,
            **({"transport": BrokerTransport(), "trust_env": False} if isolated() else {}),
            headers={
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.1",
                # Servers and CDNs routinely reject requests that omit it, and a missing
                # Accept-Language is the classic non-browser tell.
                **({"Accept-Language": accept_language} if accept_language.strip() else {}),
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def read(
        self, url: str, *, max_age_seconds: int | None = None, visual: bool = False, page: int = 1
    ) -> Document:
        # Bypass older HTML-only extraction and challenge-cache variants.
        canonical = "http:v3:" + canonicalize_url(url)
        if self.store and max_age_seconds != 0 and not visual:
            cached = await asyncio.to_thread(
                self.store.get_document,
                canonical,
                (self.cache_ttl_seconds if max_age_seconds is None else max_age_seconds),
            )
            if cached is not None and cacheable_document(cached):
                cached.warnings = [*cached.warnings, "cache_hit"]
                return cached

        current = url
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
        content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        # The native broker reports the header names it removed. A page that returns an empty
        # shell because a session cookie never arrived is otherwise indistinguishable from a
        # page that is genuinely empty, so the adjustment travels with the document.
        policy_warnings = _policy_filter_warnings(response)
        # Keep bounded HTML error bodies so render=never can report a challenge with its
        # actual status and diagnostics, and auto can attempt browser recovery.
        html_response = content_type in {"text/html", "application/xhtml+xml"} or (
            not content_type
            and response.content.lstrip().lower().startswith((b"<!doctype html", b"<html"))
        )
        if response.status_code >= 400 and not html_response:
            raise ReaderError(f"HTTP {response.status_code} for {current}")
        is_pdf = content_type == "application/pdf" or response.content.startswith(b"%PDF-")
        if is_pdf or content_type in {"image/png", "image/jpeg", "image/webp"}:
            from .documents import extract_binary

            async with self._document_limit:
                extracted = await extract_binary(
                    response.content,
                    content_type="application/pdf" if is_pdf else content_type,
                    page=page,
                    visual=visual,
                )
            extracted["content"], truncated = cap_content(
                extracted["content"], self.max_content_chars, method=extracted["method"]
            )
            if truncated:
                extracted["warnings"].append(truncated)
            document = Document(
                url=url,
                final_url=current,
                title=extracted["title"],
                content=extracted["content"],
                method=extracted["method"],
                content_type="application/pdf" if is_pdf else content_type,
                status_code=response.status_code,
                warnings=[*policy_warnings, *extracted["warnings"]],
                images=extracted["images"],
            )
            if self.store and not visual:
                await asyncio.to_thread(self.store.put_document, canonical, document)
            return document
        is_json = not html_response and (
            content_type == "application/json"
            or content_type.endswith("+json")
            or urlsplit(current).path.lower().endswith(".json")
        )
        if (
            content_type
            and not is_json
            and not (
                content_type.startswith("text/")
                or content_type in {"application/xhtml+xml", "application/xml"}
                or content_type.endswith("+xml")
            )
        ):
            raise UnsupportedContentError(f"Unsupported content type: {content_type}")

        decoded_text, decoding_warning = _decode_response(response, html_response=html_response)
        if is_json:
            try:
                content = json.dumps(json.loads(decoded_text), ensure_ascii=False, indent=2)
            except json.JSONDecodeError:
                content = decoded_text.strip()
            # Deliberately not character-capped: cutting JSON mid-string yields a payload that
            # looks structured but no longer parses. The pre-fetch max_response_bytes ceiling
            # already bounds this path, and the cache's payload ceiling evicts the rest.
            document = Document(
                url=url,
                final_url=current,
                title=_title_from_url(current),
                content=content,
                method="http+json",
                content_type=content_type,
                status_code=response.status_code,
                warnings=[*policy_warnings, *([decoding_warning] if decoding_warning else [])],
                links=[],
            )
            if self.store:
                await asyncio.to_thread(self.store.put_document, canonical, document)
            return document

        if not html_response:
            # Preserve code, Markdown, CSV, XML/RSS and whitespace verbatim. HTML parsing
            # otherwise eats literal tags and joins lines, including <placeholder> values.
            content, truncated = cap_content(
                decoded_text, self.max_content_chars, method="http+text"
            )
            document = Document(
                url=url,
                final_url=current,
                title=_title_from_url(current),
                content=content,
                method="http+text",
                content_type=content_type or "text/plain",
                status_code=response.status_code,
                warnings=[w for w in (*policy_warnings, decoding_warning, truncated) if w],
            )
            if self.store:
                await asyncio.to_thread(self.store.put_document, canonical, document)
            return document

        html_text = decoded_text
        fallback_title, fallback_content, links = extract_html_fallback(html_text, current)
        published_at, published_at_source = published_at_from_html(html_text)
        content, method, warnings = _extract_main_content(html_text, current, fallback_content)
        warnings = [*policy_warnings, *warnings]
        if decoding_warning:
            warnings.append(decoding_warning)
        content, truncated = cap_content(content, self.max_content_chars, method=method)
        if truncated:
            warnings.append(truncated)
        title = fallback_title or _title_from_url(current)
        if response.headers.get("cf-mitigated", "").lower() == "challenge":
            warnings.extend(
                [
                    "suspected_error_page:bot_challenge",
                    "browser_recommended:browser_check_interstitial",
                ]
            )
        warnings.extend(
            assess_html_quality(
                html_text,
                content,
                title=title,
                status_code=response.status_code,
            ).warnings()
        )
        warnings = list(dict.fromkeys(warnings))

        document = Document(
            url=url,
            final_url=current,
            title=title,
            content=content,
            method=method,
            published_at=published_at,
            published_at_source=published_at_source,
            content_type=content_type or "text/html",
            status_code=response.status_code,
            warnings=warnings,
            links=links[:500],
            attribution=attribution_from_html(html_text, current),
        )
        if self.store and cacheable_document(document):
            await asyncio.to_thread(self.store.put_document, canonical, document)
        return document

    async def _bounded_get(self, url: str, *, proxy_fake_dns: bool = False):
        from ..isolation.transport import MAX_BROKER_RESPONSE_BYTES, BrokerFailure

        try:
            return await self._stream_get(url, proxy_fake_dns=proxy_fake_dns)
        except BrokerFailure as exc:
            # A local decision is not a website's refusal. Saying which one it was is the
            # difference between a retryable page and an engine that gets blamed for a minute.
            if exc.kind == "too_large":
                raise ResponseTooLargeError(
                    f"Response for {url} exceeds the {MAX_BROKER_RESPONSE_BYTES} byte "
                    "network transfer limit"
                ) from exc
            if exc.kind == "cancelled":
                raise ReaderError(f"Request for {url} was cancelled before it completed") from exc
            if exc.kind == "policy":
                raise ReaderError(f"Blocked by the local network policy for {url}: {exc}") from exc
            raise ReaderError(f"Fetch failed for {url}: {exc}") from exc

    async def _stream_get(self, url: str, *, proxy_fake_dns: bool = False):
        jar = getattr(self._client.cookies, "jar", None)
        if hasattr(jar, "prune"):
            # Pruned per request rather than per read, because a redirect chain is one read and a
            # site that sets a cookie on hop one still gets it on hop two.
            jar.prune()
        async with self._client.stream("GET", url) as response:
            self._validate_connected_peer(response, proxy_fake_dns=proxy_fake_dns)
            length = response.headers.get("content-length")
            if length and int(length) > self.max_response_bytes:
                raise ResponseTooLargeError(
                    f"Response for {url} exceeds the configured "
                    f"{self.max_response_bytes} byte limit"
                )
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > self.max_response_bytes:
                    raise ResponseTooLargeError(
                        f"Response for {url} exceeds the configured "
                        f"{self.max_response_bytes} byte limit"
                    )
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


def _policy_filter_warnings(response) -> list[str]:
    """Name the headers the network policy removed, in the fixed ``-`` prefixed form it reports."""
    warnings = []
    for name in response.extensions.get("broker_filtered") or []:
        if str(name).startswith("-"):
            warnings.append(f"response_header_dropped:{name[1:]}")
        else:
            warnings.append(f"request_header_dropped:{name}")
    return warnings


def _extract_main_content(
    html_text: str, url: str, fallback_content: str
) -> tuple[str, str, list[str]]:
    warnings: list[str] = []
    try:
        import trafilatura

        extracted = trafilatura.extract(
            absolute_html_links(html_text, url),
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
    if extracted and has_meaningful_text(extracted):
        return extracted.strip(), "http+trafilatura", warnings
    warnings.append("main_extraction_fallback")
    return fallback_content, "http+basic_html", warnings


def _title_from_url(url: str) -> str:
    parsed = urlsplit(url)
    return parsed.path.rstrip("/").rsplit("/", 1)[-1] or parsed.hostname or url


def _decode_response(response, *, html_response: bool) -> tuple[str, str | None]:
    """Honor declared HTML encodings instead of assuming every page is UTF-8.

    lxml reads BOM/meta declarations without decompressing the already-bounded response again.
    The HTTP charset takes precedence. Keep a visible warning when decoding is destructive.
    """
    encoding = response.charset_encoding
    if not encoding and html_response:
        from lxml import etree

        try:
            tree = etree.fromstring(response.content, parser=etree.HTMLParser())
            encoding = tree.getroottree().docinfo.encoding if tree is not None else None
            # The HTML parser defaults to Latin-1 in the absence of a declaration. Prefer
            # UTF-8 then unless the response actually contains a legacy charset declaration.
            if b"charset" not in response.content[
                :4096
            ].lower() and not response.content.startswith((b"\xff\xfe", b"\xfe\xff")):
                encoding = "utf-8-sig"
        except (ValueError, etree.LxmlError):
            encoding = None
    try:
        return response.content.decode(encoding or "utf-8-sig"), None
    except (LookupError, UnicodeDecodeError):
        return response.content.decode("utf-8-sig", errors="replace"), "text_decoding_lossy"
