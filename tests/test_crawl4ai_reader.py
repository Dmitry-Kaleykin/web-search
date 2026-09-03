from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from web_research.readers.crawl4ai import Crawl4AIReader
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


if __name__ == "__main__":
    unittest.main()
