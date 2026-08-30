from __future__ import annotations

import gzip
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from web_research.readers.http import HTTPReader, ReaderError, _extract_main_content


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
