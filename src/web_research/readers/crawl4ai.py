from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlsplit

from ..dates import normalize_published_at
from ..models import Document
from ..safety.urls import canonicalize_url, validate_public_url
from ..storage import SQLiteStore
from .base import cap_content
from .http import ReaderError
from .quality import page_diagnostics, rendering_signals


class Crawl4AIReader:
    """JavaScript-capable reader, loaded only when the optional extra is installed."""

    def __init__(
        self,
        *,
        store: SQLiteStore | None = None,
        user_agent: str = "LocalResearchBot/0.1",
        allow_private_urls: bool = False,
        allow_proxy_fake_ips: bool = False,
        page_timeout_ms: int = 35_000,
        max_content_chars: int = 1_000_000,
        max_concurrent_renders: int = 2,
    ) -> None:
        self.store = store
        self.user_agent = user_agent
        self.allow_private_urls = allow_private_urls
        self.allow_proxy_fake_ips = allow_proxy_fake_ips
        self.page_timeout_ms = page_timeout_ms
        # Unlike the HTTP reader this path has no pre-fetch byte cap: Chromium materialises the
        # rendered document before we see it, so the ceiling has to be applied to the extracted
        # Markdown. It is the only leverage available and it is what bounds cache growth.
        self.max_content_chars = max_content_chars
        # Prefetch fans out into unbounded concurrent tasks and every caller shares one Chromium.
        # Without a ceiling a handful of simultaneous renders can exhaust memory and stall the
        # event loop of the whole server, so renders are bounded here rather than in callers that
        # cannot see each other.
        self.max_concurrent_renders = max(1, max_concurrent_renders)
        self._render_semaphore = asyncio.Semaphore(self.max_concurrent_renders)
        self._crawler: Any = None
        self._run_config: Any = None
        self._start_lock = asyncio.Lock()

    async def close(self) -> None:
        if self._crawler is not None:
            await self._crawler.close()
            self._crawler = None

    async def read(self, url: str, *, query: str | None = None) -> Document:
        await validate_public_url(
            url,
            allow_private=self.allow_private_urls,
            allow_proxy_fake_ips=self.allow_proxy_fake_ips,
        )
        crawler, run_config = await self._ensure_crawler()
        effective_config = _query_run_config(run_config, query) if query else run_config
        try:
            async with self._render_semaphore:
                result = await crawler.arun(url=url, config=effective_config)
        except Exception as exc:
            raise ReaderError(f"Crawl4AI failed for {url}: {exc}") from exc
        markdown, content_filtered = _markdown_text(result.markdown)
        recovered_structural_warning = _recoverable_structural_warning(result, markdown)
        if not result.success and not recovered_structural_warning:
            raise ReaderError(f"Crawl4AI failed for {url}: {result.error_message}")

        final_url = str(result.url or url)
        await validate_public_url(
            final_url,
            allow_private=self.allow_private_urls,
            allow_proxy_fake_ips=self.allow_proxy_fake_ips,
        )
        if not markdown.strip():
            raise ReaderError(f"Crawl4AI produced no Markdown for {url}")
        title = str((result.metadata or {}).get("title") or _title_from_url(final_url))
        warnings = ["browser_escalation"]
        if content_filtered:
            warnings.append("browser_content_filtered")
        if query:
            warnings.append("query_focused_extraction")
        if recovered_structural_warning:
            warnings.append("crawl4ai_false_positive:minimal_text")
        status_code = _status_code(result)
        warnings.extend(
            f"suspected_error_page:{reason}"
            for reason in page_diagnostics(title, markdown, status_code=status_code)
        )
        warnings.extend(
            f"browser_output_incomplete:{reason}" for reason in rendering_signals(markdown)
        )
        markdown, markdown_truncated = cap_content(
            markdown.strip(), self.max_content_chars, method="crawl4ai+chromium"
        )
        if markdown_truncated:
            warnings.append(markdown_truncated)
        document = Document(
            url=canonicalize_url(url),
            final_url=canonicalize_url(final_url),
            title=title,
            content=markdown,
            method="crawl4ai+chromium",
            published_at=_metadata_date(result.metadata or {}),
            published_at_source="crawl4ai_metadata"
            if _metadata_date(result.metadata or {})
            else None,
            content_type="text/html",
            status_code=status_code,
            warnings=warnings,
            links=_result_links(result.links),
        )
        # Query-filtered output is not a complete representation of the URL and must not replace
        # the shared URL cache used by later research questions.
        if self.store and not query:
            await asyncio.to_thread(self.store.put_document, canonicalize_url(url), document)
        return document

    async def _ensure_crawler(self):
        async with self._start_lock:
            if self._crawler is not None:
                return self._crawler, self._run_config
            try:
                from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig
                from crawl4ai.content_filter_strategy import PruningContentFilter
                from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator
            except ImportError as exc:
                raise ReaderError(
                    "Crawl4AI is unavailable; install the optional `browser` dependency"
                ) from exc

            browser_config = BrowserConfig(
                headless=True,
                verbose=False,
                user_agent=self.user_agent,
            )
            self._run_config = CrawlerRunConfig(
                cache_mode=CacheMode.BYPASS,
                check_robots_txt=True,
                page_timeout=self.page_timeout_ms,
                wait_until="domcontentloaded",
                delay_before_return_html=0.5,
                flatten_shadow_dom=True,
                word_count_threshold=1,
                markdown_generator=DefaultMarkdownGenerator(
                    content_filter=PruningContentFilter(
                        threshold=0.42,
                        threshold_type="dynamic",
                    )
                ),
            )
            self._crawler = AsyncWebCrawler(config=browser_config)
            self._install_network_guard(self._crawler)
            try:
                await self._crawler.start()
            except Exception as exc:
                self._crawler = None
                raise ReaderError(
                    "Crawl4AI could not start Chromium; run `crawl4ai-setup` and `crawl4ai-doctor`"
                ) from exc
            return self._crawler, self._run_config

    def _install_network_guard(self, crawler) -> None:
        allow_private = self.allow_private_urls
        allow_proxy_fake_ips = self.allow_proxy_fake_ips

        async def on_page_context_created(page, context, **_kwargs):
            host_cache: dict[str, bool] = {}

            async def route_filter(route):
                request = route.request
                parsed = urlsplit(request.url)
                if parsed.scheme in {"about", "blob", "data"}:
                    await route.continue_()
                    return
                host = (parsed.hostname or "").lower()
                allowed = host_cache.get(host)
                if allowed is None:
                    try:
                        await validate_public_url(
                            request.url,
                            allow_private=allow_private,
                            allow_proxy_fake_ips=allow_proxy_fake_ips,
                        )
                        allowed = True
                    except ValueError:
                        allowed = False
                    host_cache[host] = allowed
                if not allowed or request.resource_type in {"font", "image", "media"}:
                    await route.abort()
                else:
                    await route.continue_()

            await context.route("**", route_filter)
            return page

        crawler.crawler_strategy.set_hook("on_page_context_created", on_page_context_created)


def _markdown_text(value: Any) -> tuple[str, bool]:
    if isinstance(value, str):
        return value, False
    fit = getattr(value, "fit_markdown", None)
    if isinstance(fit, str) and fit.strip():
        return fit, True
    raw = getattr(value, "raw_markdown", None)
    return str(raw or ""), False


def _query_run_config(run_config: Any, query: str) -> Any:
    try:
        from crawl4ai.content_filter_strategy import BM25ContentFilter
        from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator

        return run_config.clone(
            markdown_generator=DefaultMarkdownGenerator(
                content_filter=BM25ContentFilter(user_query=query, bm25_threshold=0.7)
            )
        )
    except (AttributeError, ImportError, TypeError):
        return run_config


def _recoverable_structural_warning(result: Any, markdown: str) -> bool:
    """Accept Crawl4AI's known small-page false positive without masking real blocks."""
    if result.success or not markdown.strip():
        return False
    status_code = getattr(result, "status_code", None)
    if not isinstance(status_code, int) or not 200 <= status_code < 300:
        return False
    message = str(getattr(result, "error_message", "") or "")
    return message.startswith(
        "Blocked by anti-bot protection: Structural: minimal_text on small page"
    )


def _result_links(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return []
    links: list[str] = []
    for group in ("internal", "external"):
        entries = value.get(group, [])
        if not isinstance(entries, list):
            continue
        for item in entries:
            href = item.get("href") if isinstance(item, dict) else None
            if isinstance(href, str) and href.startswith(("http://", "https://")):
                links.append(href)
    return list(dict.fromkeys(links))[:500]


def _metadata_date(metadata: dict[str, Any]) -> str | None:
    for key in ("date", "published_time", "article:published_time"):
        value = metadata.get(key)
        if value:
            return normalize_published_at(value)
    return None


def _status_code(result: Any) -> int | None:
    value = getattr(result, "status_code", None)
    return value if isinstance(value, int) else None


def _title_from_url(url: str) -> str:
    parsed = urlsplit(url)
    return parsed.path.rstrip("/").rsplit("/", 1)[-1] or parsed.hostname or url
