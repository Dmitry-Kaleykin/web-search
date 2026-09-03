from __future__ import annotations

import gzip
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from web_research.readers.base import cap_content
from web_research.readers.http import HTTPReader, ReaderError, _extract_main_content
from web_research.storage import SQLiteStore


class ContentCapTests(unittest.IsolatedAsyncioTestCase):
    def test_text_within_the_ceiling_is_untouched(self) -> None:
        content, warning = cap_content("short text", 100, method="http+trafilatura")
        self.assertEqual(content, "short text")
        self.assertIsNone(warning)

    def test_overlong_text_is_capped_and_the_cap_is_visible(self) -> None:
        content, warning = cap_content("x" * 500, 100, method="crawl4ai+chromium")
        self.assertEqual(len(content), 100)
        self.assertEqual(warning, "content_truncated:500>100:crawl4ai+chromium")

    def test_a_non_positive_ceiling_disables_capping(self) -> None:
        content, warning = cap_content("x" * 500, 0, method="http+json")
        self.assertEqual(len(content), 500)
        self.assertIsNone(warning)

    async def test_reader_caches_exactly_the_text_it_returns(self) -> None:
        """Cached and live copies must be byte-identical: excerpt verification depends on it."""
        import tempfile
        from pathlib import Path

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                text=(
                    "<html><body><article><p>"
                    + "alpha beta gamma " * 2000
                    + "</p></article></body></html>"
                ),
                headers={"content-type": "text/html; charset=utf-8"},
            )

        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "cache.sqlite3")
            reader = HTTPReader(store=store, allow_private_urls=True, max_content_chars=300)
            await reader._client.aclose()
            reader._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            try:
                live = await reader.read("http://127.0.0.1/long-article")
                cached = store.get_document("http://127.0.0.1/long-article", 3600)
            finally:
                await reader.close()
                store.close()

        self.assertEqual(len(live.content), 300)
        self.assertTrue(
            any(w.startswith("content_truncated:") for w in live.warnings), live.warnings
        )
        self.assertIsNotNone(cached)
        self.assertEqual(cached.content, live.content)

    async def test_json_content_is_not_character_capped_because_that_corrupts_it(self) -> None:
        import json

        payload = {"data": "y" * 5000}

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload, headers={"content-type": "application/json"})

        reader = HTTPReader(allow_private_urls=True, max_content_chars=200)
        await reader._client.aclose()
        reader._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            document = await reader.read("http://127.0.0.1/big.json")
        finally:
            await reader.close()

        self.assertGreater(len(document.content), 200)
        self.assertEqual(json.loads(document.content), payload)
        self.assertFalse(
            any(w.startswith("content_truncated:") for w in document.warnings),
            document.warnings,
        )



class HTTPReaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_short_valid_trafilatura_result_is_not_discarded(self) -> None:
        fake_trafilatura = SimpleNamespace(extract=lambda *_args, **_kwargs: "Version 4.2")

        with patch.dict(sys.modules, {"trafilatura": fake_trafilatura}):
            content, method, warnings = _extract_main_content(
                "<html><p>Version 4.2</p></html>",
                "https://example.com/release",
                "Version 4.2",
            )

        self.assertEqual(content, "Version 4.2")
        self.assertEqual(method, "http+trafilatura")
        self.assertNotIn("main_extraction_fallback", warnings)

    async def test_empty_trafilatura_result_uses_basic_extraction(self) -> None:
        fake_trafilatura = SimpleNamespace(extract=lambda *_args, **_kwargs: "   ")

        with patch.dict(sys.modules, {"trafilatura": fake_trafilatura}):
            content, method, warnings = _extract_main_content(
                "<html><p>Fallback text</p></html>",
                "https://example.com/release",
                "Fallback text",
            )

        self.assertEqual(content, "Fallback text")
        self.assertEqual(method, "http+basic_html")
        self.assertIn("main_extraction_fallback", warnings)

    async def test_reader_extracts_html_with_bounded_mock_transport(self) -> None:
        html = """
        <html><head><title>Example page</title></head><body>
        <nav>Navigation noise</nav>
        <main><h1>Verified result</h1>
        <p>The verified value is 42, according to the published experiment.</p>
        <p>This second paragraph provides enough substantive text for extraction and testing.</p>
        <p>A third paragraph describes the methodology and confirms the reported result.</p></main>
        </body></html>
        """

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                request=request,
                headers={"Content-Type": "text/html; charset=utf-8"},
                content=html.encode(),
            )

        reader = HTTPReader(allow_private_urls=True)
        await reader._client.aclose()
        reader._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            document = await reader.read("http://127.0.0.1/article")
        finally:
            await reader.close()
        self.assertEqual(document.title, "Example page")
        self.assertIn("verified value is 42", document.content)
        self.assertIn(document.method, {"http+trafilatura", "http+basic_html"})
        self.assertEqual(document.status_code, 200)

    async def test_reader_accepts_structured_json_evidence(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                request=request,
                # raw.githubusercontent.com commonly serves repository JSON as text/plain.
                headers={"Content-Type": "text/plain"},
                json={
                    "support": {
                        "chrome": {"version_added": "125"},
                        "firefox": {"version_added": "147"},
                    }
                },
            )

        reader = HTTPReader(allow_private_urls=True)
        await reader._client.aclose()
        reader._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            document = await reader.read("http://127.0.0.1/compat.json")
        finally:
            await reader.close()

        self.assertEqual(document.method, "http+json")
        self.assertIn('"version_added": "147"', document.content)
        self.assertEqual(document.content_type, "text/plain")

    async def test_reader_marks_cloudflare_soft_error_page(self) -> None:
        html = """
        <html><head><title>pi.dev | 525: SSL handshake failed</title></head>
        <body><main><h1>SSL handshake failed</h1>
        <p>Cloudflare is unable to establish an SSL connection to the origin server.</p>
        </main></body></html>
        """

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                request=request,
                headers={"Content-Type": "text/html"},
                content=html.encode(),
            )

        reader = HTTPReader(allow_private_urls=True)
        await reader._client.aclose()
        reader._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            document = await reader.read("http://127.0.0.1/cloudflare-error")
        finally:
            await reader.close()

        self.assertEqual(document.status_code, 200)
        self.assertIn("suspected_error_page:cloudflare_525", document.warnings)

    async def test_reader_extracts_publication_date_with_provenance(self) -> None:
        html = """
        <html><head><title>Dated page</title>
        <script type="application/ld+json">
        {"@type":"NewsArticle","datePublished":"2026-08-25T12:30:00Z"}
        </script></head><body><main><p>Substantive dated article content.</p></main></body></html>
        """

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                request=request,
                headers={"Content-Type": "text/html"},
                content=html.encode(),
            )

        reader = HTTPReader(allow_private_urls=True)
        await reader._client.aclose()
        reader._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            document = await reader.read("http://127.0.0.1/dated")
        finally:
            await reader.close()

        self.assertEqual(document.published_at, "2026-08-25T12:30:00+00:00")
        self.assertEqual(document.published_at_source, "json_ld:datePublished")

    async def test_reader_marks_empty_application_shell_for_browser(self) -> None:
        html = (
            "<html><head><title>Application</title></head><body>"
            "<nav>Products</nav><div id='root'></div>"
            "<script src='/application.js'></script></body></html>"
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                request=request,
                headers={"Content-Type": "text/html"},
                content=html.encode(),
            )

        reader = HTTPReader(allow_private_urls=True)
        await reader._client.aclose()
        reader._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            document = await reader.read("http://127.0.0.1/application")
        finally:
            await reader.close()

        self.assertIn("browser_recommended:empty_app_shell", document.warnings)

    async def test_reader_enforces_response_size_limit(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, request=request, content=b"x" * 200)

        reader = HTTPReader(allow_private_urls=True, max_response_bytes=100)
        await reader._client.aclose()
        reader._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            with self.assertRaises(ReaderError):
                await reader.read("http://127.0.0.1/large")
        finally:
            await reader.close()

    async def test_reader_does_not_decode_compressed_content_twice(self) -> None:
        html = (
            b"<html><main><h1>Compressed page</h1><p>"
            + b"useful text " * 30
            + b"</p></main></html>"
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                request=request,
                headers={"Content-Type": "text/html", "Content-Encoding": "gzip"},
                content=gzip.compress(html),
            )

        reader = HTTPReader(allow_private_urls=True)
        await reader._client.aclose()
        reader._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            document = await reader.read("http://127.0.0.1/compressed")
        finally:
            await reader.close()
        self.assertIn("Compressed page", document.content)

    async def test_proxy_fake_peer_is_allowed_only_with_explicit_option(self) -> None:
        stream = SimpleNamespace(get_extra_info=lambda _name: ("198.18.0.49", 443))
        response = SimpleNamespace(extensions={"network_stream": stream})

        strict_reader = HTTPReader()
        try:
            with self.assertRaisesRegex(ReaderError, "Connected peer is not public"):
                strict_reader._validate_connected_peer(response)
        finally:
            await strict_reader.close()

        proxy_reader = HTTPReader(allow_proxy_fake_ips=True)
        try:
            proxy_reader._validate_connected_peer(response, proxy_fake_dns=True)
        finally:
            await proxy_reader.close()

    async def test_proxy_option_does_not_allow_unvalidated_loopback_peer(self) -> None:
        stream = SimpleNamespace(get_extra_info=lambda _name: ("127.0.0.1", 443))
        response = SimpleNamespace(extensions={"network_stream": stream})
        reader = HTTPReader(allow_proxy_fake_ips=True)
        try:
            with self.assertRaisesRegex(ReaderError, "Connected peer is not public"):
                reader._validate_connected_peer(response, proxy_fake_dns=False)
            reader._validate_connected_peer(response, proxy_fake_dns=True)
        finally:
            await reader.close()


if __name__ == "__main__":
    unittest.main()
