from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import subprocess
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..dates import attribution_from_html, normalize_published_at
from ..models import Document
from ..safety.urls import validate_public_url
from ..storage import SQLiteStore
from .actions import (
    NAVIGATION_SETTLE_MS,
    ActionRun,
    action_script,
    challenge_frames,
    discover_actions,
    plan_actions,
    run_page_actions,
    wait_for_challenge_frame,
    wait_out_challenge,
)
from .base import cap_content
from .http import ReaderError
from .quality import assess_html_quality, page_diagnostics

LOGGER = logging.getLogger(__name__)

# One Chromium serves concurrent reads and crawl4ai hands its navigation hook only its own
# arguments, so a read's action plan and its outcome travel in a context variable set around the
# `arun` call. The object is mutated rather than the variable rebound, which is what lets the
# calling task see what the hook did even if the library runs a step in a child task.
_ACTION_RUN: ContextVar[ActionRun | None] = ContextVar("web_search_action_run", default=None)

# Anti-bot layers grade whether a profile is internally consistent more than whether any single
# property looks scripted. These defaults travel together: Crawl4AI derives `sec-ch-ua` from the
# User-Agent, Playwright-Stealth rewrites the JavaScript identity and pins `navigator.platform` to
# Win32, and the language header has to agree with `navigator.languages`. A macOS User-Agent would
# contradict the pinned platform string, so the Windows profile is the coherent pairing here.
DEFAULT_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)
# A maximized Chrome window on the most common desktop resolution, expressed the way a site can
# observe it. Playwright derives `screen` from the viewport, so a viewport set to the full screen
# reports a window with no taskbar, no tab strip and no address bar: `innerWidth == outerWidth ==
# screen.width` and `availHeight == screen.height`, which no real maximized window produces. The
# three numbers below are one arithmetic chain (screen, minus a 40px Windows taskbar, minus about
# 80px of browser chrome) and are patched together for the same reason.
_SCREEN = (1366, 768)
_WORK_AREA_HEIGHT = 728
_VIEWPORT = (1366, 648, 1)
# Crawl4AI builds `Stealth()` with no arguments, which leaves Playwright-Stealth's own defaults
# ("Intel Inc." / "Intel Iris OpenGL Engine") in place. Those are macOS strings sitting underneath
# a profile that claims Windows -- the exact cross-layer mismatch the ranking grades. A Windows
# Chrome reports the ANGLE adapter string here, while the *masked* `gl.VENDOR` stays "WebKit" in
# both real Chrome and this build (already measured), because that one comes from the GLES layer.
_GPU_VENDOR = "Google Inc. (Intel)"
_GPU_RENDERER = "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"
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
    (
        "const d=(o,k,v)=>Object.defineProperty(o,k,{get:()=>v,configurable:true});"
        f"d(screen,'width',{_SCREEN[0]});d(screen,'height',{_SCREEN[1]});"
        f"d(screen,'availWidth',{_SCREEN[0]});d(screen,'availHeight',{_WORK_AREA_HEIGHT});"
        f"d(window,'outerWidth',{_SCREEN[0]});d(window,'outerHeight',{_WORK_AREA_HEIGHT});"
    ),
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


def _fonts_blocked() -> bool:
    """Whether this read may skip webfonts.

    A page that is only being turned into text does not need its glyphs, and fonts are one of the
    largest remaining bandwidth items. A page that is about to be clicked, or drawn for a model to
    aim at, is being judged on how it looks, so it gets them: a screenshot the model uses to choose
    coordinates has to show what the page shows a person.
    """

    active = _ACTION_RUN.get()
    return not (active and active.interactive)


# How long a rendered read waits for a challenge widget's own document to appear. This runs on every
# rendered read, and the loop gives up as soon as a page has finished loading without naming a
# widget. Do not raise this to cover slow widgets: measured on the reCAPTCHA demo, Google attaches
# its iframe about two seconds in, and paying two seconds per render to report a name the caller can
# already aim at (`frame: "recaptcha/api2"`) is the worse trade. A click waits longer for a frame it
# was told to find (FRAME_WAIT_MS in readers/actions.py).
FRAME_DISCOVERY_WAIT_MS = 1_200


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
        challenge_grace_seconds: float = 8.0,
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
        # A managed interstitial clears itself in two to six seconds. Zero disables the wait for
        # callers that would rather have the challenge reported immediately.
        self.challenge_grace_seconds = max(0.0, challenge_grace_seconds)
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
        script_plan, page_plan = plan_actions(actions or [])
        run = ActionRun(plan=page_plan)
        # Query selection belongs at the output window, after the complete extraction has
        # been snapshotted. BM25 filtering here permanently removed context from pagination.
        effective_config = run_config
        if visual or script_plan:
            # Only these two need a different run configuration. The challenge wait and the
            # real-input actions work through the navigation hook, so a page-only plan leaves the
            # shared config alone.
            effective_config = effective_config.clone(
                screenshot=visual,
                force_viewport_screenshot=True,
                js_code=action_script(script_plan) if script_plan else None,
            )
        token = _ACTION_RUN.set(run)
        try:
            async with self._render_semaphore:
                result = await crawler.arun(url=url, config=effective_config)
        except Exception as exc:
            raise ReaderError(f"Crawl4AI failed for {url}: {exc}") from exc
        finally:
            _ACTION_RUN.reset(token)
        script_actions_failed = bool(script_plan) and not _actions_completed(
            getattr(result, "js_execution_result", None), len(script_plan)
        )
        # A failed action is reported, not fatal. The page that was fetched is still the page the
        # caller asked to read; what did not happen is the interaction. Failing the read instead
        # would drop a rendered document onto the plain HTTP reader and lose the content the caller
        # can use, which is what an escalation to a browser was meant to avoid.
        action_warnings = [f"action_failed:{reason}"[:160] for reason in run.failures[:3]]
        if script_actions_failed:
            action_warnings.append("action_failed:script reported no completed action")
        markdown, content_filtered = _markdown_text(result.markdown)
        recovered_structural_warning = _recoverable_structural_reason(result, markdown)
        if not result.success and not recovered_structural_warning:
            raise ReaderError(f"Crawl4AI failed for {url}: {result.error_message}")

        final_url = str(getattr(result, "redirected_url", None) or result.url or url)
        await validate_public_url(
            final_url,
            allow_private=self.allow_private_urls,
            allow_proxy_fake_ips=self.allow_proxy_fake_ips,
        )
        if not markdown.strip() and not (visual and getattr(result, "screenshot", None)):
            raise ReaderError(f"Crawl4AI produced no Markdown for {url}")
        title = str((result.metadata or {}).get("title") or _title_from_url(final_url))
        warnings = ["browser_escalation"]
        warnings.extend(run.warnings)
        if run.challenge_wait_ms or run.challenge_cleared is False:
            # The wait is part of why the answer arrived (or did not), so it belongs in the record
            # rather than in a log line the caller cannot see.
            warnings.append(f"challenge_waited:{run.challenge_wait_ms}")
            warnings.append(
                "challenge_cleared" if run.challenge_cleared else "challenge_still_present"
            )
        warnings.extend(f"challenge_frame:{frame}" for frame in run.challenge_frames)
        warnings.extend(action_warnings)
        if content_filtered:
            warnings.append("browser_content_filtered")
        if recovered_structural_warning:
            warnings.append(f"crawl4ai_false_positive:{recovered_structural_warning}")
        status_code = _status_code(result)
        warnings.extend(
            f"suspected_error_page:{reason}"
            for reason in page_diagnostics(title, markdown, status_code=status_code)
        )
        assessment = assess_html_quality(
            str(getattr(result, "html", "") or ""),
            markdown,
            title=title,
            status_code=status_code,
        )
        warnings.extend(
            f"browser_output_incomplete:{reason}" for reason in assessment.browser_reasons
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
                markdown_generator=DefaultMarkdownGenerator(),
            )
            last_error: Exception | None = None
            # Try the configured build, then the library default: an uninstalled browser build is
            # a configuration gap, not a reason to lose the page.
            for channel in dict.fromkeys((self.channel, "")):
                crawler = AsyncWebCrawler(config=self._browser_config(channel))
                self._install_network_guard(crawler)
                self._install_action_hook(crawler)
                self._install_launch_tweaks(crawler, channel)
                self._install_stealth_profile(crawler)
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

    def _install_action_hook(self, crawler) -> None:
        """Give a self-clearing challenge its grace, then carry out this read's real-input actions.

        `after_goto` is the only point where the document exists, has settled enough to inspect and
        has not been extracted or screenshotted yet. Everything here runs before the script actions,
        so a click that opens a panel is visible to the `expand`/`load_more` step that follows.
        """

        async def after_goto(page, **_kwargs):
            run = _ACTION_RUN.get()
            if run is None:
                return
            if self.challenge_grace_seconds > 0:
                run.challenge_wait_ms, run.challenge_cleared = await wait_out_challenge(
                    page, self.challenge_grace_seconds
                )
            # Reported whether or not the challenge is still up: a widget the caller can aim at is
            # worth knowing about even when the page decided to answer anyway. The widget's own
            # document is attached after the load event, so an empty list costs a short wait.
            await wait_for_challenge_frame(page, run, FRAME_DISCOVERY_WAIT_MS)
            arrived = str(page.url)
            if run.plan:
                await run_page_actions(page, run)
                # A click can open the document it was aimed at, and the next read cannot aim at a
                # frame it was never told about.
                run.challenge_frames = [
                    *run.challenge_frames,
                    *(f for f in challenge_frames(page) if f not in run.challenge_frames),
                ][:3]
                if run.navigated_to and run.navigated_to != arrived:
                    # The extraction and screenshot that follow should see the page the click
                    # arrived at. Crawl4AI settles navigations it performs itself; a click made here
                    # is not one of those.
                    settle = getattr(page, "wait_for_load_state", None)
                    if settle is not None:
                        # The arrived page may still be streaming its own resources when the bound
                        # runs out; the read proceeds with whatever has arrived.
                        with contextlib.suppress(Exception):
                            await settle("domcontentloaded", timeout=NAVIGATION_SETTLE_MS)

        crawler.crawler_strategy.set_hook("after_goto", after_goto)

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
        from ..isolation.transport import isolated

        if isolated():
            kwargs.update(create_isolated_context=True, ignore_https_errors=False)
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
        if isolated():
            kwargs.setdefault("extra_args", []).extend(
                [
                    "--disable-background-networking",
                    "--disable-quic",
                    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                ]
            )
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

    def _install_stealth_profile(self, crawler) -> None:
        """Give the stealth helper GPU strings that match the claimed operating system.

        ``StealthAdapter`` instantiates ``Stealth()`` with no arguments, so the library's macOS
        renderer defaults survive under a User-Agent, ``navigator.platform`` and UA-Client-Hints
        that all say Windows. Substituting the adapter's instance keeps the values inside the
        library's own patch instead of racing it from an init script: whichever `getParameter`
        wrapper runs last wins, and the library's is installed per page, after context scripts.
        """
        if not self.stealth:
            return
        try:
            adapter = crawler.crawler_strategy.browser_manager._stealth_adapter
            current = adapter._stealth
        except AttributeError:
            return
        if current is None:
            return
        try:
            from playwright_stealth import Stealth
        except ImportError:
            return
        # Every other evasion keeps its default; only the two renderer strings change.
        adapter._stealth = Stealth(
            webgl_vendor_override=_GPU_VENDOR,
            webgl_renderer_override=_GPU_RENDERER,
        )

    def _install_network_guard(self, crawler) -> None:
        from ..isolation.transport import isolated

        if isolated():
            self._install_broker_guard(crawler)
            return
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
                blocked = {"media"} | ({"font"} if _fonts_blocked() else set())
                if not allowed or request.resource_type in blocked:
                    await route.abort()
                else:
                    await route.continue_()

            await context.route("**", route_filter)
            return page

        crawler.crawler_strategy.set_hook("on_page_context_created", on_page_context_created)

    def _install_broker_guard(self, crawler) -> None:
        """Fulfill browser requests through the native broker; never use route.continue_."""
        import httpx

        from ..isolation.transport import MAX_REQUEST_BODY_BYTES, BrokerTransport

        manager = crawler.crawler_strategy.browser_manager
        create_context = manager.create_browser_context
        context_lock = asyncio.Lock()

        async def guarded_context(*args, **kwargs):
            # Crawl4AI 0.9.2 does not expose Playwright's service_workers setting.
            # Interpose new_context only while its serialized context creation runs.
            async with context_lock:
                return await create_guarded(*args, **kwargs)

        async def create_guarded(*args, **kwargs):
            browser = manager.browser
            original = browser.new_context

            async def new_context(**options):
                options.update(
                    service_workers="block", accept_downloads=False, ignore_https_errors=False
                )
                return await original(**options)

            browser.new_context = new_context
            try:
                return await create_context(*args, **kwargs)
            finally:
                browser.new_context = original

        manager.create_browser_context = guarded_context

        async def on_page_context_created(page, context, **_kwargs):
            await context.clear_cookies()
            _filtered_seen: set[tuple] = set()

            async def block_socket(route):
                await route.close()

            await context.route_web_socket("**", block_socket)

            async def broker_route(route):
                request = route.request
                blocked = {"media"} | ({"font"} if _fonts_blocked() else set())
                if request.resource_type in blocked:
                    await route.abort()
                    return
                body = request.post_data_buffer
                # A rendered page may talk to its own API. A cross-origin POST could turn what
                # the page read into an outbound request to an unrelated host, and an unbounded
                # body would turn a render into an upload.
                outside_own_origin = request.method == "POST" and (
                    not _same_origin(request.url, request.frame.url)
                    or body is None
                    or len(body) > MAX_REQUEST_BODY_BYTES
                )
                if (
                    request.method not in {"GET", "HEAD", "POST"}
                    or outside_own_origin
                    or (request.method != "POST" and body)
                ):
                    await route.abort()
                    return
                try:
                    # A fresh client per request: BrokerTransport opens its own socket per request
                    # anyway, and the browser context gives no teardown point at which to close a
                    # shared client safely. The timeout is not optional: httpx hands its timeout
                    # to the transport instead of enforcing it, so without one every stalled
                    # subresource would wait for the broker's full 75-second budget.
                    async with httpx.AsyncClient(
                        transport=BrokerTransport(),
                        trust_env=False,
                        timeout=httpx.Timeout(25.0, connect=10.0),
                    ) as client:
                        result = await client.request(
                            request.method,
                            request.url,
                            headers=request.headers,
                            content=body if request.method == "POST" else None,
                        )
                    await route.fulfill(
                        status=result.status_code, headers=dict(result.headers), body=result.content
                    )
                    dropped = result.extensions.get("broker_filtered") or []
                    if dropped:
                        # Once per distinct set per context: what gets filtered is a property of
                        # the policy, not of one request, and a page issues many of each.
                        key = tuple(dropped)
                        if key not in _filtered_seen:
                            _filtered_seen.add(key)
                            LOGGER.warning(
                                "Network broker filtered browser request headers for %s: %s",
                                request.url,
                                ", ".join(dropped),
                            )
                except Exception:
                    await route.abort()

            await context.route("**", broker_route)
            return page

        crawler.crawler_strategy.set_hook("on_page_context_created", on_page_context_created)


def _markdown_text(value: Any) -> tuple[str, bool]:
    if isinstance(value, str):
        return value, False
    raw = getattr(value, "raw_markdown", None)
    if isinstance(raw, str) and raw.strip():
        return raw, False
    fit = getattr(value, "fit_markdown", None)
    return (fit, True) if isinstance(fit, str) else ("", False)


def _same_origin(request_url: str, frame_url: str) -> bool:
    """Whether a subresource shares the origin of the frame that requested it."""

    def origin(value: str) -> tuple:
        parsed = urlsplit(value)
        default = 443 if parsed.scheme == "https" else 80
        return (parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port or default)

    return origin(request_url) == origin(frame_url)


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
    # Crawl4AI retains the first hop's status separately from the final response.
    value = getattr(result, "redirected_status_code", None)
    if not isinstance(value, int):
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
