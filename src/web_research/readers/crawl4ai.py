from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..dates import attribution_from_html, normalize_published_at
from ..models import Document
from ..safety.urls import validate_public_url
from ..storage import SQLiteStore
from .actions import action_script, discover_actions
from .base import cap_content
from .http import ReaderError
from .quality import page_diagnostics, rendering_signals

LOGGER = logging.getLogger(__name__)

# Anti-bot layers grade whether a profile is internally consistent more than whether any single
# property looks scripted. These defaults travel together: Crawl4AI derives `sec-ch-ua` from the
# User-Agent, Playwright-Stealth rewrites the JavaScript identity and pins `navigator.platform` to
# Win32, and the language header has to agree with `navigator.languages`. A macOS User-Agent would
# contradict the pinned platform string, so the Windows profile is the coherent pairing here.
DEFAULT_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)
_VIEWPORT = (1366, 768, 1)
_GPU_DISABLE_FLAGS = frozenset(
    {"--disable-gpu", "--disable-gpu-compositing", "--disable-software-rasterizer"}
)
# Chromium reports the host's real memory here, and a 16/32 GiB value is outside the enumeration
# the Device Memory spec allows. Headless also answers `denied` for notification permission where
# a fresh real profile answers `prompt`; Playwright-Stealth mirrors this property into
# `permissions.query`, so patching it moves both APIs together instead of trading one signal for
# the other.
_STEALTH_INIT_SCRIPTS = (
    "Object.defineProperty(navigator,'deviceMemory',{get:()=>8});",
    "Object.defineProperty(Notification,'permission',{get:()=>'prompt'});",
)
_STRUCTURAL_SINGLE_SIGNALS = frozenset(
    {"minimal_text", "no_content_elements", "script_heavy_shell"}
)
_PROXY_SCHEMES = frozenset({"http", "https", "socks4", "socks5", "socks5h"})
_CHANNEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_VERSION_MAJOR_RE = re.compile(r"(\d+)(?:\.\d+)*")
_LOCALE_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")


def _chromium_major_version() -> int | None:
    """Read the major version from the Playwright Chromium the reader will actually launch."""
    roots = [
        os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""),
        str(Path(os.environ.get("CRAWL4_AI_BASE_DIRECTORY", ".web-search-data")) / "ms-playwright"),
    ]
    candidates: list[Path] = []
    for root in (item for item in roots if item):
        base = Path(root).expanduser()
        candidates.extend(
            sorted(base.glob("chromium-*/chrome-*/Google Chrome for Testing.app/Contents/MacOS/*"))
            + sorted(base.glob("chromium-*/chrome-linux*/chrome"))
            + sorted(base.glob("chromium-*/chrome-headless-shell-*/*-headless-shell"))
        )
    for executable in reversed(candidates):
        # Fixed argv: the browser binary comes from this project's own store.
        try:
            reported = subprocess.run(
                [str(executable), "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        match = _VERSION_MAJOR_RE.search(reported)
        if match:
            return int(match.group(1))
    return None


@lru_cache(maxsize=8)
def resolve_browser_user_agent(configured: str = "") -> str:
    """Prefer an operator-supplied User-Agent, else match the installed Chromium's major version."""
    if configured:
        return configured
    major = _chromium_major_version()
    if major is None:
        return DEFAULT_BROWSER_USER_AGENT
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
    )


def _locale_from_accept_language(value: str) -> str | None:
    """Take the primary language tag, so `--lang` cannot carry arbitrary flag characters."""
    primary = value.split(",", 1)[0].split(";", 1)[0].strip()
    return primary if _LOCALE_RE.fullmatch(primary) else None


class Crawl4AIReader:
    """JavaScript-capable reader, loaded only when the optional extra is installed."""

    def __init__(
        self,
        *,
        store: SQLiteStore | None = None,
        browser_user_agent: str = "",
        accept_language: str = "en-US,en;q=0.9",
        stealth: bool = True,
        channel: str = "",
        proxy: str = "",
        allow_private_urls: bool = False,
        allow_proxy_fake_ips: bool = False,
        page_timeout_ms: int = 35_000,
        max_content_chars: int = 1_000_000,
        max_concurrent_renders: int = 2,
        render_semaphore: asyncio.Semaphore | None = None,
    ) -> None:
        self.store = store
        self.browser_user_agent = browser_user_agent
        self.accept_language = accept_language.strip()
        self.stealth = stealth
        self.channel = channel.strip() if _CHANNEL_RE.fullmatch(channel.strip()) else ""
        self.channel_in_use = channel
        self.proxy = proxy.strip()
        self.browser_identity = resolve_browser_user_agent(browser_user_agent)
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
        self._render_semaphore = render_semaphore or asyncio.Semaphore(self.max_concurrent_renders)
        self._crawler: Any = None
        self._run_config: Any = None
        self._start_lock = asyncio.Lock()

    async def close(self) -> None:
        if self._crawler is not None:
            await self._crawler.close()
            self._crawler = None

    async def read(
        self,
        url: str,
        *,
        query: str | None = None,
        visual: bool = False,
        actions: list[dict] | None = None,
    ) -> Document:
        await validate_public_url(
            url,
            allow_private=self.allow_private_urls,
            allow_proxy_fake_ips=self.allow_proxy_fake_ips,
        )
        crawler, run_config = await self._ensure_crawler()
        effective_config = _query_run_config(run_config, query) if query else run_config
        if visual or actions:
            effective_config = effective_config.clone(
                screenshot=visual,
                force_viewport_screenshot=True,
                js_code=action_script(actions) if actions else None,
            )
        try:
            async with self._render_semaphore:
                result = await crawler.arun(url=url, config=effective_config)
        except Exception as exc:
            raise ReaderError(f"Crawl4AI failed for {url}: {exc}") from exc
        if actions and not _actions_completed(
            getattr(result, "js_execution_result", None), len(actions)
        ):
            raise ReaderError("Read-only browser action failed or targeted an unsupported control")
        markdown, content_filtered = _markdown_text(result.markdown)
        recovered_structural_warning = _recoverable_structural_reason(result, markdown)
        if not result.success and not recovered_structural_warning:
            raise ReaderError(f"Crawl4AI failed for {url}: {result.error_message}")

        final_url = str(result.url or url)
        await validate_public_url(
            final_url,
            allow_private=self.allow_private_urls,
            allow_proxy_fake_ips=self.allow_proxy_fake_ips,
        )
        if not markdown.strip() and not (visual and getattr(result, "screenshot", None)):
            raise ReaderError(f"Crawl4AI produced no Markdown for {url}")
        title = str((result.metadata or {}).get("title") or _title_from_url(final_url))
        warnings = ["browser_escalation"]
        if content_filtered:
            warnings.append("browser_content_filtered")
        if query:
            warnings.append("query_focused_extraction")
        if recovered_structural_warning:
            warnings.append(f"crawl4ai_false_positive:{recovered_structural_warning}")
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
            url=url,
            final_url=final_url,
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
            available_actions=discover_actions(str(getattr(result, "html", "") or "")),
            attribution=attribution_from_html(str(getattr(result, "html", "") or ""), final_url),
        )
        if visual and getattr(result, "screenshot", None):
            import base64
            import io

            from PIL import Image

            from .documents import image_block

            raw = base64.b64decode(result.screenshot)
            if len(raw) > 8_000_000:
                raise ReaderError("Screenshot exceeds image budget")
            with Image.open(io.BytesIO(raw)) as image:
                document.images = [image_block(image, 1)]
            document.warnings.append("visual_viewport_only:use scroll actions to inspect more")
        return document

    async def _ensure_crawler(self):
        async with self._start_lock:
            if self._crawler is not None:
                return self._crawler, self._run_config
            try:
                from crawl4ai import AsyncWebCrawler, CacheMode, CrawlerRunConfig
                from crawl4ai.content_filter_strategy import PruningContentFilter
                from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator
            except ImportError as exc:
                raise ReaderError(
                    "Crawl4AI is unavailable; install the optional `browser` dependency"
                ) from exc

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
            last_error: Exception | None = None
            # Try the configured build, then the library default: an uninstalled browser build is
            # a configuration gap, not a reason to lose the page.
            for channel in dict.fromkeys((self.channel, "")):
                crawler = AsyncWebCrawler(config=self._browser_config(channel))
                self._install_network_guard(crawler)
                self._install_launch_tweaks(crawler, channel)
                try:
                    await crawler.start()
                except Exception as exc:
                    last_error = exc
                    with contextlib.suppress(Exception):
                        await crawler.close()
                    if channel:
                        LOGGER.warning(
                            "Chromium build %r failed to start; retrying with default", channel
                        )
                    continue
                self._crawler = crawler
                self.channel_in_use = channel
                return self._crawler, self._run_config
            self._crawler = None
            raise ReaderError(
                "Crawl4AI could not start Chromium; run `crawl4ai-setup` and `crawl4ai-doctor`"
            ) from last_error

    def _browser_config(self, channel: str | None = None):
        """Build the Chromium profile. Every identity layer has to agree with the others."""
        from crawl4ai import BrowserConfig

        channel = self.channel if channel is None else channel
        kwargs: dict[str, Any] = {
            "headless": True,
            "verbose": False,
            "user_agent": self.browser_identity,
            # 1080x600 is neither a real screen nor a real window; it is the library default.
            "viewport_width": _VIEWPORT[0],
            "viewport_height": _VIEWPORT[1],
            "device_scale_factor": _VIEWPORT[2],
        }
        if self.accept_language:
            # Crawl4AI merges these into the request headers it sets from the User-Agent, so
            # Accept-Language lands alongside sec-ch-ua instead of replacing it.
            kwargs["headers"] = {"Accept-Language": self.accept_language}
        if self.stealth:
            kwargs["enable_stealth"] = True
            locale = _locale_from_accept_language(self.accept_language)
            if locale:
                kwargs["extra_args"] = [f"--lang={locale}"]
            kwargs["init_scripts"] = list(_STEALTH_INIT_SCRIPTS)
        # Anything but the literal "chromium" reaches Playwright through Crawl4AI's own
        # `chrome_channel` field. That value names the new-headless build, and Crawl4AI withholds
        # it on Windows, so it goes through the launch arguments instead.
        if channel and channel != "chromium":
            kwargs["chrome_channel"] = channel
        proxy = _proxy_config(self.proxy)
        if proxy is not None:
            kwargs["proxy_config"] = proxy
        return BrowserConfig(**kwargs)

    def _install_launch_tweaks(self, crawler, channel: str = "") -> None:
        """Adjust the launch arguments Crawl4AI builds for the browser process.

        Two things are corrected here, both about the process launch rather than the page:

        * GPU-disabling flags. Crawl4AI hard-codes ``--disable-gpu`` and friends into its dedicated
          launch path, which leaves ``canvas.getContext('webgl')`` null. A desktop Chrome never
          reports that, so the absence is a signal however clean the rest of the profile is.
          Stealth mode already keeps SwiftShader alive on the managed launch path; this brings the
          same exception to the default one.
        * The requested browser build when it is the new-headless ``chromium`` channel, which
          Crawl4AI deliberately never forwards.

        The launch helper is private to the installed Crawl4AI, so a build that moves it simply
        keeps the previous behaviour.
        """
        try:
            manager = crawler.crawler_strategy.browser_manager
            build_args = manager._build_browser_args
        except AttributeError:
            return

        drop_gpu_flags = self.stealth
        use_new_headless = channel == "chromium"
        if not drop_gpu_flags and not use_new_headless:
            return

        def build_adjusted_args():
            args = build_args()
            if drop_gpu_flags:
                args["args"] = [
                    flag for flag in args.get("args", ()) if flag not in _GPU_DISABLE_FLAGS
                ]
            if use_new_headless:
                args["channel"] = "chromium"
            return args

        manager._build_browser_args = build_adjusted_args

    def _install_network_guard(self, crawler) -> None:
        allow_private = self.allow_private_urls
        allow_proxy_fake_ips = self.allow_proxy_fake_ips

        async def on_page_context_created(page, context, **_kwargs):
            # Crawl4AI plants a `cookiesEnabled` cookie the site never issued, so it arrives on the
            # very first request as a crawler marker. Remove that one cookie only: clearance
            # cookies a challenge page sets later in the same read have to survive.
            with contextlib.suppress(Exception):  # an older browser API must not fail the read
                await context.clear_cookies(name="cookiesEnabled")
            host_cache: dict[str, bool] = {}

            async def route_filter(route):
                request = route.request
                if request.method not in {"GET", "HEAD", "OPTIONS"}:
                    await route.abort()
                    return
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
                if not allowed or request.resource_type in {"font", "media"}:
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


def _proxy_config(value: str):
    """Translate a proxy URL into Crawl4AI's ProxyConfig, rejecting unusable schemes.

    A rendered page fetches through this proxy, so the proxy - not this process - resolves names
    for the browser. That is the point of it (network origin is the one signal no fingerprint
    patch can move), but it also means `validate_public_url`'s local resolution no longer
    describes the browser's actual peer, so the proxy stays an explicit operator choice.
    """
    if not value:
        return None
    parsed = urlsplit(value)
    if parsed.scheme not in _PROXY_SCHEMES or not parsed.hostname:
        return None
    from crawl4ai import ProxyConfig

    port = f":{parsed.port}" if parsed.port else ""
    return ProxyConfig(
        server=f"{parsed.scheme}://{parsed.hostname}{port}",
        username=parsed.username or None,
        password=parsed.password or None,
    )


def _recoverable_structural_reason(result: Any, markdown: str) -> str | None:
    """Name Crawl4AI's single-signal small-page block suspicion when rendering disproves it.

    The library treats any one weak structural signal on a page under 5000 bytes as a silent
    anti-bot block: `minimal_text`, `no_content_elements`, or `script_heavy_shell`. A short page
    that carries real extracted Markdown contradicts that guess, so the read proceeds with the
    reason recorded. Two or more signals stay fatal; they are the shape of a real interstitial.
    """
    if result.success or not markdown.strip():
        return None
    status_code = getattr(result, "status_code", None)
    if not isinstance(status_code, int) or not 200 <= status_code < 300:
        return None
    message = str(getattr(result, "error_message", "") or "")
    if not message.startswith("Blocked by anti-bot protection: Structural:"):
        return None
    _, _, remainder = message.partition("Structural: ")
    reason, _, details = remainder.partition(" ")
    if not details.startswith("on small page"):
        return None
    reason = reason.rstrip(".,")
    return reason if reason in _STRUCTURAL_SINGLE_SIGNALS else None


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


def _actions_completed(value: Any, count: int) -> bool:
    if isinstance(value, dict):
        if value.get("success") is False:
            return False
        if value.get("read_actions_completed") == count:
            return True
        return any(_actions_completed(child, count) for child in value.values())
    if isinstance(value, list):
        return bool(value) and all(_actions_completed(child, count) for child in value)
    return False
