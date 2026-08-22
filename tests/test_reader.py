from __future__ import annotations

import unittest

import httpx

from web_research.readers.http import HTTPReader, ReaderError


class HTTPReaderTests(unittest.IsolatedAsyncioTestCase):
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


if __name__ == "__main__":
    unittest.main()
