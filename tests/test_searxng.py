from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import httpx

from web_research.search.searxng import SearXNGSearchProvider
from web_research.storage import SQLiteStore


class SearXNGSearchProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_transient_results_are_not_cached(self):
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"results": []})

        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "cache.sqlite3")
            provider = SearXNGSearchProvider("http://searxng.test", store=store)
            await provider._client.aclose()
            provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            try:
                first = await provider.search("temporarily unavailable")
                second = await provider.search("temporarily unavailable")
            finally:
                await provider.close()
                store.close()

        self.assertEqual(first, [])
        self.assertEqual(second, [])
        self.assertEqual(len(requests), 2)

    async def test_json_contract_deduplication_and_cache(self):
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "url": "https://example.com/item?utm_source=test",
                            "title": " Item ",
                            "content": "Summary",
                            "engines": ["brave", "duckduckgo"],
                            "publishedDate": "2026-08-01",
                            "score": 1.5,
                        },
                        {
                            "url": "https://example.com/item",
                            "title": "Duplicate",
                        },
                    ]
                },
            )

        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "cache.sqlite3")
            provider = SearXNGSearchProvider("http://searxng.test", store=store)
            await provider._client.aclose()
            provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            try:
                first = await provider.search(
                    "product facts", page=2, language="en", time_range="month", limit=5
                )
                second = await provider.search(
                    "product facts", page=2, language="en", time_range="month", limit=5
                )
            finally:
                await provider.close()
                store.close()

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].url.params["format"], "json")
        self.assertEqual(requests[0].url.params["pageno"], "2")
        self.assertEqual(requests[0].url.params["language"], "en")
        self.assertEqual(requests[0].url.params["time_range"], "month")
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].url, "https://example.com/item")
        self.assertEqual(first[0].engines, ["brave", "duckduckgo"])
        self.assertEqual(second, first)


if __name__ == "__main__":
    unittest.main()
