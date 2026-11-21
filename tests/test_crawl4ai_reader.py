from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from web_research.readers.crawl4ai import (
    _GPU_RENDERER,
    _GPU_VENDOR,
    _SCREEN,
    _VIEWPORT,
    _WORK_AREA_HEIGHT,
    Crawl4AIReader,
    _proxy_config,
    resolve_browser_user_agent,
)
from web_research.readers.http import ReaderError


def crawl_result(**overrides):
    values = {
        "success": True,
        "status_code": 200,
        "error_message": "",
        "url": "http://127.0.0.1/product",
        "markdown": SimpleNamespace(raw_markdown="Rendered details"),
        "metadata": {"title": "Product"},
        "links": {},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class Crawl4AIReaderTests(unittest.IsolatedAsyncioTestCase):
    async def reader_for(self, result):
        reader = Crawl4AIReader(allow_private_urls=True)
        crawler = SimpleNamespace(arun=AsyncMock(return_value=result))
        reader._ensure_crawler = AsyncMock(return_value=(crawler, object()))
        return reader

    async def test_renders_are_bounded_concurrently(self) -> None:
        """Prefetch fans out freely and every caller shares one Chromium, so renders are capped."""
        import asyncio

        inflight = 0
        peak = 0

        async def arun(**_kwargs):
            nonlocal inflight, peak
            inflight += 1
            peak = max(peak, inflight)
            await asyncio.sleep(0.01)
            inflight -= 1
            return crawl_result()

        reader = Crawl4AIReader(allow_private_urls=True, max_concurrent_renders=1)
        crawler = SimpleNamespace(arun=arun)
        reader._ensure_crawler = AsyncMock(return_value=(crawler, object()))

        documents = await asyncio.gather(
            *[reader.read(f"http://127.0.0.1/{index}") for index in range(4)]
        )

        self.assertEqual(len(documents), 4)
        self.assertEqual(peak, 1)

    async def test_recovers_minimal_text_false_positive_with_rendered_markdown(self):
        result = crawl_result(
            success=False,
            error_message=(
                "Blocked by anti-bot protection: Structural: minimal_text on small page "
                "(300 bytes, 40 chars visible)"
            ),
        )
        reader = await self.reader_for(result)

        document = await reader.read("http://127.0.0.1/product")

        self.assertEqual(document.content, "Rendered details")
        self.assertEqual(document.status_code, 200)
        self.assertIn("crawl4ai_false_positive:minimal_text", document.warnings)

    async def test_rejects_high_confidence_anti_bot_failure(self):
        result = crawl_result(
            success=False,
            error_message="Blocked by anti-bot protection: Cloudflare challenge",
        )
        reader = await self.reader_for(result)

        with self.assertRaisesRegex(ReaderError, "Cloudflare challenge"):
            await reader.read("http://127.0.0.1/product")

    async def test_rejects_minimal_text_warning_without_markdown(self):
        result = crawl_result(
            success=False,
            error_message=(
                "Blocked by anti-bot protection: Structural: minimal_text on small page"
            ),
            markdown=SimpleNamespace(raw_markdown=""),
        )
        reader = await self.reader_for(result)

        with self.assertRaisesRegex(ReaderError, "minimal_text"):
            await reader.read("http://127.0.0.1/product")

    async def test_preserves_raw_markdown_so_filtering_cannot_remove_context(self):
        result = crawl_result(
            markdown=SimpleNamespace(
                raw_markdown="Navigation\n\nBrowser support details\n\nException: preview only.",
                fit_markdown="Browser support details",
            )
        )
        reader = await self.reader_for(result)

        document = await reader.read("http://127.0.0.1/product")

        self.assertIn("Exception: preview only.", document.content)
        self.assertNotIn("browser_content_filtered", document.warnings)

    async def test_query_does_not_replace_full_extraction_with_filtered_subset(self):
        result = crawl_result()
        reader = await self.reader_for(result)
        await reader.read("http://127.0.0.1/product", query="details")
        crawler, config = await reader._ensure_crawler()
        self.assertIs(crawler.arun.call_args.kwargs["config"], config)

    async def test_redirect_reports_final_url_and_response_status(self):
        reader = await self.reader_for(
            crawl_result(
                status_code=301,
                redirected_url="http://127.0.0.1/new-product",
                redirected_status_code=200,
            )
        )
        document = await reader.read("http://127.0.0.1/product")
        self.assertEqual(document.final_url, "http://127.0.0.1/new-product")
        self.assertEqual(document.status_code, 200)


class BrowserProfileTests(unittest.IsolatedAsyncioTestCase):
    """The rendered profile has to stay internally consistent or it reads as scripted."""

    def test_identity_layers_agree(self) -> None:
        reader = Crawl4AIReader(browser_user_agent="", accept_language="en-US,en;q=0.9")
        config = reader._browser_config()

        self.assertTrue(config.enable_stealth)
        self.assertNotIn("LocalResearchBot", config.user_agent)
        self.assertIn("Windows", config.user_agent)
        self.assertEqual(config.headers["Accept-Language"], "en-US,en;q=0.9")
        self.assertEqual(["--lang=en-US"], config.extra_args)
        self.assertEqual(1366, config.viewport_width)
        self.assertEqual(_VIEWPORT[1], config.viewport_height)
        self.assertEqual(3, len(config.init_scripts))

    def test_window_geometry_describes_a_real_window(self) -> None:
        """A maximized window leaves room for a taskbar and browser chrome; headless had neither.

        The chain has to stay arithmetically plausible in both gaps, and the viewport must not
        equal the screen, which is what made the old profile read as a window with no chrome.
        """
        reader = Crawl4AIReader()
        config = reader._browser_config()
        taskbar = _SCREEN[1] - _WORK_AREA_HEIGHT
        browser_chrome = _WORK_AREA_HEIGHT - _VIEWPORT[1]

        self.assertTrue(0 < taskbar <= 80, taskbar)
        self.assertTrue(40 < browser_chrome <= 120, browser_chrome)
        self.assertNotEqual(_SCREEN[1], _VIEWPORT[1])
        self.assertEqual(_SCREEN[0], _VIEWPORT[0])  # maximized windows have no side chrome
        joined = "".join(config.init_scripts)
        for value in (str(_SCREEN[1]), str(_WORK_AREA_HEIGHT)):
            self.assertIn(value, joined)

    def test_gpu_strings_agree_with_the_claimed_platform(self) -> None:
        """Crawl4AI's arg-less Stealth() defaults to macOS GPU strings under a Windows profile."""
        from playwright_stealth import Stealth

        reader = Crawl4AIReader()
        adapter = SimpleNamespace(_stealth=Stealth())
        crawler = SimpleNamespace(
            crawler_strategy=SimpleNamespace(
                browser_manager=SimpleNamespace(_stealth_adapter=adapter)
            )
        )
        before = adapter._stealth.webgl_renderer_override

        reader._install_stealth_profile(crawler)

        self.assertNotEqual(before, adapter._stealth.webgl_renderer_override)
        self.assertEqual(_GPU_RENDERER, adapter._stealth.webgl_renderer_override)
        self.assertEqual(_GPU_VENDOR, adapter._stealth.webgl_vendor_override)
        self.assertIn("Windows", reader._browser_config().user_agent)
        self.assertIn("ANGLE", adapter._stealth.webgl_renderer_override)
        # Substituting the instance must not silently change any other evasion.
        self.assertEqual(Stealth().navigator_platform, adapter._stealth.navigator_platform)

    def test_stealth_off_leaves_the_adapter_alone(self) -> None:
        reader = Crawl4AIReader(stealth=False)
        adapter = SimpleNamespace(_stealth="sentinel")
        crawler = SimpleNamespace(
            crawler_strategy=SimpleNamespace(
                browser_manager=SimpleNamespace(_stealth_adapter=adapter)
            )
        )

        reader._install_stealth_profile(crawler)

        self.assertEqual("sentinel", adapter._stealth)

    def test_missing_stealth_adapter_is_not_a_failure(self) -> None:
        """A Crawl4AI build that moves the private adapter simply keeps the old behaviour."""
        reader = Crawl4AIReader()
        crawler = SimpleNamespace(crawler_strategy=SimpleNamespace())

        reader._install_stealth_profile(crawler)

    def test_user_agent_matches_the_installed_browser(self) -> None:
        detected = resolve_browser_user_agent("")

        self.assertNotIn("LocalResearchBot", detected)
        self.assertNotIn("Headless", detected)
        self.assertEqual(detected, resolve_browser_user_agent(""))
        self.assertEqual("CustomAgent/9", resolve_browser_user_agent("CustomAgent/9"))

    def test_gpu_disable_flags_are_dropped_so_webgl_keeps_a_renderer(self) -> None:
        reader = Crawl4AIReader()
        launched: dict = {}

        class Manager:
            def _build_browser_args(self):
                return {
                    "headless": True,
                    "args": [
                        "--disable-gpu",
                        "--disable-gpu-compositing",
                        "--disable-software-rasterizer",
                        "--no-sandbox",
                    ],
                }

        class Strategy:
            browser_manager = Manager()

        crawler = SimpleNamespace(crawler_strategy=Strategy())
        reader._install_launch_tweaks(crawler)
        launched.update(crawler.crawler_strategy.browser_manager._build_browser_args())

        self.assertEqual(["--no-sandbox"], launched["args"])

    def test_new_headless_channel_needs_the_launch_argument(self) -> None:
        """Crawl4AI never forwards channel='chromium', so it has to be injected."""
        reader = Crawl4AIReader(channel="chromium")
        manager = SimpleNamespace(_build_browser_args=lambda: {"args": ["--disable-gpu"]})
        reader._install_launch_tweaks(
            SimpleNamespace(crawler_strategy=SimpleNamespace(browser_manager=manager)), "chromium"
        )

        launched = manager._build_browser_args()
        self.assertEqual("chromium", launched["channel"])
        self.assertEqual([], launched["args"])
        # A build other than that one travels through Crawl4AI's own field instead.
        self.assertEqual(
            "chromium-headless-shell",
            Crawl4AIReader(channel="chromium-headless-shell")._browser_config().chrome_channel,
        )

    def test_unusable_channels_and_proxies_are_ignored(self) -> None:
        self.assertEqual("", Crawl4AIReader(channel="../../sbin/init").channel)
        self.assertIsNone(_proxy_config(""))
        self.assertIsNone(_proxy_config("ftp://cache.internal:21"))
        self.assertIsNone(_proxy_config("http://"))

        proxy = _proxy_config("http://user:pass@cache.internal:8888")
        self.assertEqual("http://cache.internal:8888", proxy.server)
        self.assertEqual(("user", "pass"), (proxy.username, proxy.password))

    def test_proxy_reaches_the_browser_config(self) -> None:
        config = Crawl4AIReader(proxy="socks5h://gateway.internal:1080")._browser_config()

        self.assertEqual("socks5h://gateway.internal:1080", config.proxy_config.server)

    def test_stealth_off_restores_library_defaults(self) -> None:
        reader = Crawl4AIReader(stealth=False)
        config = reader._browser_config()

        self.assertFalse(config.enable_stealth)
        self.assertEqual([], config.extra_args)
        self.assertEqual([], config.init_scripts)

        manager = SimpleNamespace(_build_browser_args=lambda: {"args": ["--disable-gpu"]})
        reader._install_launch_tweaks(
            SimpleNamespace(crawler_strategy=SimpleNamespace(browser_manager=manager))
        )
        self.assertEqual(["--disable-gpu"], manager._build_browser_args()["args"])

    def test_language_tag_cannot_inject_browser_flags(self) -> None:
        config = Crawl4AIReader(accept_language="*/*;--evil=1")._browser_config()

        self.assertEqual([], config.extra_args)

    async def test_unavailable_build_falls_back_to_the_default_channel(self) -> None:
        """An uninstalled browser build must not cost the caller the page."""
        reader = Crawl4AIReader(allow_private_urls=True, channel="chrome-beta")
        started: list[dict] = []
        attempts = iter([RuntimeError("no such channel"), None])

        class Crawler:
            def __init__(self, config):
                self.config = config
                self.crawler_strategy = SimpleNamespace(set_hook=lambda *_args: None)

            async def start(self):
                error = next(attempts)
                started.append(self.config.chrome_channel)
                if error:
                    raise error

            async def close(self):
                return None

        import crawl4ai

        with patch.object(crawl4ai, "AsyncWebCrawler", Crawler):
            crawler, _ = await reader._ensure_crawler()

        self.assertEqual(["chrome-beta", reader._browser_config("").chrome_channel], started)
        self.assertIs(crawler, reader._crawler)
        self.assertEqual("", reader.channel_in_use)

    async def test_persistent_start_failure_is_still_an_error(self) -> None:
        reader = Crawl4AIReader(allow_private_urls=True)

        class Crawler:
            def __init__(self, config):
                self.crawler_strategy = SimpleNamespace(set_hook=lambda *_args: None)

            async def start(self):
                raise RuntimeError("chrome vanished")

            async def close(self):
                return None

        import crawl4ai

        with (
            patch.object(crawl4ai, "AsyncWebCrawler", Crawler),
            self.assertRaisesRegex(ReaderError, "could not start Chromium"),
        ):
            await reader._ensure_crawler()
        self.assertIsNone(reader._crawler)


class StructuralFalsePositiveTests(unittest.IsolatedAsyncioTestCase):
    async def reader_for(self, result):
        reader = Crawl4AIReader(allow_private_urls=True)
        reader._ensure_crawler = AsyncMock(
            return_value=(SimpleNamespace(arun=AsyncMock(return_value=result)), object())
        )
        return reader

    async def read(self, error_message):
        result = crawl_result(success=False, error_message=error_message)
        return await (await self.reader_for(result)).read("http://127.0.0.1/short")

    async def test_recovers_any_single_structural_signal(self) -> None:
        document = await self.read(
            "Blocked by anti-bot protection: Structural: no_content_elements on small page "
            "(1019 bytes, 959 chars visible)"
        )

        self.assertEqual("Rendered details", document.content)
        self.assertIn("crawl4ai_false_positive:no_content_elements", document.warnings)

    async def test_multiple_structural_signals_stay_fatal(self) -> None:
        with self.assertRaisesRegex(ReaderError, "minimal_text"):
            await self.read(
                "Blocked by anti-bot protection: Structural: minimal_text, no_content_elements "
                "(900 bytes, 12 chars visible)"
            )

    async def test_non_structural_block_reason_is_not_renamed(self) -> None:
        with self.assertRaisesRegex(ReaderError, "no <body> tag"):
            await self.read("Blocked by anti-bot protection: Structural: no <body> tag (400 bytes)")


class WidgetFrame:
    """The challenge document. It owns the click, not the page that embedded it."""

    def __init__(self, url: str, owner: Widget) -> None:
        self.url = url
        self.owner = owner

    def locator(self, _selector: str):
        return self.owner._handle(_selector)

    def get_by_text(self, _text: str, *, exact: bool):
        return self.owner.get_by_text(_text, exact=exact)


class Widget:
    """A page whose challenge control lives in another document, as Playwright would show it."""

    def __init__(self, *, anchor_url: str, click_fails=False, text_visible=False) -> None:
        self.url = "http://127.0.0.1/form"
        self.click_fails = click_fails
        self.text_visible = text_visible
        self.clicked: list[str] = []
        self.slept: list[int] = []
        self.main_frame = self
        self.frames = [self, WidgetFrame(anchor_url, self)]

    async def title(self) -> str:
        return "Sign in"

    async def evaluate(self, _script: str) -> str:
        return ""

    async def wait_for_timeout(self, ms: int) -> None:
        self.slept.append(ms)

    async def wait_for_load_state(self, _state: str, *, timeout: int) -> None:
        pass

    def _handle(self, selector: str):
        outer = self

        class Handle:
            @property
            def first(self):
                return self

            async def evaluate(self, _script: str, *, timeout: int = 1_500) -> bool:
                return False

            async def scroll_into_view_if_needed(self, *, timeout: int) -> None:
                pass

            async def click(self, *, timeout: int) -> None:
                if outer.click_fails:
                    raise TimeoutError("locator.click: Timeout 3000ms exceeded")
                outer.clicked.append(selector)

        return Handle()

    def locator(self, selector: str):
        return self._handle(selector)

    def get_by_text(self, _text: str, *, exact: bool):
        outer = self

        class Text:
            @property
            def first(self):
                return self

            async def wait_for(self, *, state: str, timeout: int) -> None:
                if not outer.text_visible:
                    raise TimeoutError(f"waiting for {state} text")

        return Text()


class Crawl4AIActionHookTests(unittest.IsolatedAsyncioTestCase):
    """The hook is where a click happens; the read is where its consequences have to surface."""

    async def reader_with_page(self, page, *, js_result=None):
        hooks: dict = {}

        class Strategy:
            def set_hook(self, name, fn) -> None:
                hooks[name] = fn

        reader = Crawl4AIReader(allow_private_urls=True, challenge_grace_seconds=1)
        reader._install_action_hook(SimpleNamespace(crawler_strategy=Strategy()))

        async def arun(**_kwargs):
            await hooks["after_goto"](page)
            return crawl_result(js_execution_result=js_result)

        crawler = SimpleNamespace(arun=arun)
        config = SimpleNamespace(clone=lambda **kwargs: SimpleNamespace(**kwargs))
        reader._ensure_crawler = AsyncMock(return_value=(crawler, config))
        return reader

    async def test_a_failed_click_is_reported_on_the_page_it_could_not_act_on(self) -> None:
        """The page is still worth returning; what failed was the interaction, not the read."""
        page = Widget(
            anchor_url="https://www.google.com/recaptcha/api2/anchor?k=6Le-x", click_fails=True
        )
        reader = await self.reader_with_page(page)

        document = await reader.read(
            "http://127.0.0.1/form",
            actions=[{"kind": "click", "selector": "input", "frame": "google.com"}],
        )

        self.assertEqual("Rendered details", document.content)
        self.assertEqual(page.clicked, [])
        self.assertTrue(
            any(
                warning.startswith("action_failed:click TimeoutError: locator.click")
                for warning in document.warnings
            ),
            document.warnings,
        )

    async def test_a_script_action_that_did_nothing_is_reported_the_same_way(self) -> None:
        page = Widget(anchor_url="https://site.example/panel")
        reader = await self.reader_with_page(page, js_result={"read_actions_completed": 0})

        document = await reader.read(
            "http://127.0.0.1/form", actions=[{"kind": "expand", "selector": "details"}]
        )

        self.assertIn("action_failed:script reported no completed action", document.warnings)

    async def test_a_widget_frame_is_told_to_the_caller_without_guessing_for_it(self) -> None:
        page = Widget(anchor_url="https://challenges.cloudflare.com/turnstile/widget?sitekey=x")
        reader = await self.reader_with_page(page)

        document = await reader.read(
            "http://127.0.0.1/form",
            actions=[{"kind": "wait_for", "text": "Sign in confirmed", "duration_ms": 400}],
        )

        self.assertIn(
            "challenge_frame:https://challenges.cloudflare.com/turnstile/widget", document.warnings
        )
        self.assertIn("action_timeout:wait_for:Sign in confirmed", document.warnings)

    async def test_a_mixed_plan_is_judged_on_the_actions_script_could_run(self) -> None:
        """The click runs outside the page script, so the script's own count must not include it."""
        page = Widget(anchor_url="https://site.example/panel")
        reader = await self.reader_with_page(page, js_result={"read_actions_completed": 1})

        document = await reader.read(
            "http://127.0.0.1/form",
            actions=[
                {"kind": "expand", "selector": "details"},
                {"kind": "click", "selector": "button"},
            ],
        )

        self.assertEqual(page.clicked, ["button"])
        self.assertEqual(document.content, "Rendered details")

    async def test_the_plan_does_not_outlive_the_read_that_supplied_it(self) -> None:
        from web_research.readers.crawl4ai import _ACTION_RUN

        page = Widget(anchor_url="https://site.example/panel")
        reader = await self.reader_with_page(page)
        await reader.read(
            "http://127.0.0.1/form", actions=[{"kind": "click", "selector": "button"}]
        )

        self.assertIsNone(_ACTION_RUN.get())


if __name__ == "__main__":
    unittest.main()
