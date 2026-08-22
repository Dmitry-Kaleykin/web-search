from __future__ import annotations

import unittest

from web_research.models import Document
from web_research.readers.router import LayeredReader


class FakeReader:
    def __init__(self, result=None, error=None) -> None:
        self.result = result
        self.error = error
        self.calls = 0
        self.closed = False

    async def read(self, _url):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result

    async def close(self):
        self.closed = True


def document(content: str, method: str) -> Document:
    return Document(
        url="https://example.com",
        final_url="https://example.com",
        title="Example",
        content=content,
        method=method,
    )


class LayeredReaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_http_page_does_not_launch_browser(self) -> None:
        primary = FakeReader(document("Enough substantive text. " * 40, "http"))
        browser = FakeReader(document("Rendered", "browser"))
        router = LayeredReader(primary, browser)
        result = await router.read("https://example.com")
        self.assertEqual(result.method, "http")
        self.assertEqual(browser.calls, 0)

    async def test_short_http_page_escalates_to_browser(self) -> None:
        primary = FakeReader(document("Enable JavaScript", "http"))
        browser = FakeReader(document("Rendered content " * 40, "crawl4ai+chromium"))
        router = LayeredReader(primary, browser)
        result = await router.read("https://example.com")
        self.assertEqual(result.method, "crawl4ai+chromium")
        self.assertEqual(browser.calls, 1)

    async def test_browser_failure_preserves_usable_primary_result(self) -> None:
        primary = FakeReader(document("Short but usable", "http"))
        browser = FakeReader(error=RuntimeError("not installed"))
        router = LayeredReader(primary, browser)
        result = await router.read("https://example.com")
        self.assertEqual(result.method, "http")
        self.assertTrue(any(item.startswith("browser_fallback_failed") for item in result.warnings))


if __name__ == "__main__":
    unittest.main()
