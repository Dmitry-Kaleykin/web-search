from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from web_research.readers.crawl4ai import (
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

    async def test_prefers_filtered_markdown_over_navigation_heavy_raw_markdown(self):
        result = crawl_result(
            markdown=SimpleNamespace(
                raw_markdown="Navigation " * 200,
                fit_markdown="Browser support details",
            )
        )
        reader = await self.reader_for(result)

        document = await reader.read("http://127.0.0.1/product")

        self.assertEqual(document.content, "Browser support details")
        self.assertIn("browser_content_filtered", document.warnings)


class BrowserProfileTests(unittest.TestCase):
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
        self.assertEqual(2, len(config.init_scripts))

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

        self.assertEqual(["chrome-beta", None], started)  # retried on the library default
        self.assertIs(crawler, reader._crawler)
        self.assertEqual("", reader.channel_in_use)

    async def test_persistent_start_failure_is_still_an_error(self) -> None:
        reader = Crawl4AIReader(allow_private_urls=True)

        class Crawler:
            def __init__(self, _config):
                pass

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


if __name__ == "__main__":
    unittest.main()
