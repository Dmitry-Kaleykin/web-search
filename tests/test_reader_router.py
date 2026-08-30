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

    async def test_javascript_shell_escalates_to_browser(self) -> None:
        primary = FakeReader(document("Enable JavaScript", "http"))
        browser = FakeReader(document("Rendered content " * 40, "crawl4ai+chromium"))
        router = LayeredReader(primary, browser)
        result = await router.read("https://example.com")
        self.assertEqual(result.method, "crawl4ai+chromium")
        self.assertEqual(browser.calls, 1)

    async def test_short_substantive_page_does_not_launch_browser(self) -> None:
        primary = FakeReader(document("Version 4.2 released today.", "http"))
        browser = FakeReader(document("Rendered", "browser"))
        router = LayeredReader(primary, browser)
        result = await router.read("https://example.com")
        self.assertEqual(result.method, "http")
        self.assertEqual(browser.calls, 0)

    async def test_http_quality_warning_escalates_to_browser(self) -> None:
        primary_document = document("Documentation", "http")
        primary_document.warnings.append("browser_recommended:empty_app_shell")
        primary = FakeReader(primary_document)
        browser = FakeReader(document("Rendered documentation", "crawl4ai+chromium"))
        router = LayeredReader(primary, browser)
        result = await router.read("https://example.com")
        self.assertEqual(result.method, "crawl4ai+chromium")
        self.assertEqual(browser.calls, 1)

    async def test_render_never_preserves_http_shell_without_launching_browser(self) -> None:
        primary_document = document("Enable JavaScript", "http")
        primary = FakeReader(primary_document)
        browser = FakeReader(document("Rendered", "crawl4ai+chromium"))
        router = LayeredReader(primary, browser)

        result = await router.read("https://example.com", render="never")

        self.assertEqual(result.method, "http")
        self.assertEqual(browser.calls, 0)
        self.assertIn("browser_render_skipped:render_never", result.warnings)

    async def test_render_always_launches_browser_for_complete_http_page(self) -> None:
        primary = FakeReader(document("Complete static page", "http"))
        browser = FakeReader(document("Rendered page", "crawl4ai+chromium"))
        router = LayeredReader(primary, browser)

        result = await router.read("https://example.com", render="always")

        self.assertEqual(result.method, "crawl4ai+chromium")
        self.assertEqual(browser.calls, 1)

    async def test_browser_failure_preserves_primary_result(self) -> None:
        primary_document = document("Navigation", "http")
        primary_document.warnings.append("browser_recommended:empty_app_shell")
        primary = FakeReader(primary_document)
        browser = FakeReader(error=RuntimeError("not installed"))
        router = LayeredReader(primary, browser)
        result = await router.read("https://example.com")
        self.assertEqual(result.method, "http")
        self.assertTrue(any(item.startswith("browser_fallback_failed") for item in result.warnings))


if __name__ == "__main__":
    unittest.main()
