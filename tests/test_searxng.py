from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

import httpx

from web_research.search.searxng import (
    SearXNGChallengeError,
    SearXNGError,
    SearXNGSearchProvider,
)
from web_research.storage import SQLiteStore


class SearXNGSearchProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_unresponsive_engines_are_retried_and_reported(self):
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={"results": [], "unresponsive_engines": [["brave", "timeout"]]},
            )

        provider = SearXNGSearchProvider(
            "http://searxng.test",
            max_retries=2,
            retry_base_seconds=0,
        )
        await provider._client.aclose()
        provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            with self.assertRaisesRegex(SearXNGError, "upstream engines were unresponsive"):
                await provider.search("browser support")
        finally:
            await provider.close()

        self.assertEqual(len(requests), 3)
        self.assertEqual(provider.last_warnings, ["search_engines_unresponsive:brave: timeout"])

    async def test_partial_engine_failure_is_exposed_with_usable_results(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "results": [{"url": "https://example.com", "title": "Example"}],
                    "unresponsive_engines": [["google", "captcha"]],
                },
            )

        provider = SearXNGSearchProvider("http://searxng.test")
        await provider._client.aclose()
        provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            results = await provider.search("example")
        finally:
            await provider.close()

        self.assertEqual(len(results), 1)
        self.assertEqual(provider.last_warnings, ["search_engines_unresponsive:google: captcha"])

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
                    "product facts",
                    page=2,
                    language="en",
                    time_range="month",
                    categories="science",
                    limit=5,
                )
                second = await provider.search(
                    "product facts",
                    page=2,
                    language="en",
                    time_range="month",
                    categories="science",
                    limit=5,
                )
            finally:
                await provider.close()
                store.close()

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].url.params["format"], "json")
        self.assertEqual(requests[0].url.params["pageno"], "2")
        self.assertEqual(requests[0].url.params["language"], "en")
        self.assertEqual(requests[0].url.params["time_range"], "month")
        self.assertEqual(requests[0].url.params["categories"], "science")
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].url, "https://example.com/item")
        self.assertEqual(first[0].engines, ["brave", "duckduckgo"])
        self.assertEqual(second, first)

    async def test_anti_bot_html_page_fails_fast_without_retrying(self):
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                text='<!doctype html><title>Verifying your browser</title>'
                '<noscript><meta http-equiv="refresh" content="0; url=/antibot/captcha">',
                headers={"content-type": "text/html; charset=utf-8"},
            )

        provider = SearXNGSearchProvider("http://searxng.test", retry_base_seconds=0)
        await provider._client.aclose()
        provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            with self.assertRaisesRegex(SearXNGChallengeError, "anti-bot page"):
                await provider.search("anything")
        finally:
            await provider.close()

        self.assertEqual(len(requests), 1)

    async def test_forbidden_json_request_is_reported_as_configuration_not_retried(self):
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(403, text="Forbidden")

        provider = SearXNGSearchProvider("http://searxng.test", retry_base_seconds=0)
        await provider._client.aclose()
        provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            with self.assertRaisesRegex(SearXNGChallengeError, "search.formats"):
                await provider.search("anything")
        finally:
            await provider.close()

        self.assertEqual(len(requests), 1)

    async def test_retry_after_beyond_budget_keeps_earlier_results(self):
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.params.get("engines"):
                return httpx.Response(429, headers={"retry-after": "600"}, text="slow down")
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"url": f"https://example.com/{index}", "engines": ["google cse"]}
                        for index in range(4)
                    ],
                    "unresponsive_engines": [["duckduckgo", "CAPTCHA"]],
                },
            )

        provider = SearXNGSearchProvider(
            "http://searxng.test",
            retry_base_seconds=0,
            max_retry_wait_seconds=10.0,
            healthy_engines="mwmbl",
        )
        await provider._client.aclose()
        provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            results = await provider.search("context engineering")
        finally:
            await provider.close()

        self.assertEqual(len(requests), 2)
        self.assertEqual(len(results), 4)
        self.assertTrue(
            any("retry_after 600s" in warning for warning in provider.last_warnings),
            provider.last_warnings,
        )
        self.assertIn("searxng", provider.engine_health())

    async def test_diversity_collapse_requeries_pinned_to_healthy_engines(self):
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.params.get("engines"):
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            {"url": "https://mwmbl.example/one", "engines": ["mwmbl"]},
                            {"url": "https://searchmysite.example/two", "engines": ["mojeek"]},
                        ]
                    },
                )
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"url": f"https://cse.example/{index}", "engines": ["google cse"]}
                        for index in range(4)
                    ],
                    "unresponsive_engines": [["duckduckgo", "CAPTCHA"]],
                },
            )

        provider = SearXNGSearchProvider(
            "http://searxng.test",
            retry_base_seconds=0,
            healthy_engines="mwmbl,mojeek",
        )
        await provider._client.aclose()
        provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            results = await provider.search("context engineering")
        finally:
            await provider.close()

        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0].url.params.get("engines"), None)
        self.assertEqual(requests[1].url.params["engines"], "mwmbl,mojeek")
        self.assertEqual(len(results), 6)
        self.assertEqual([result.rank for result in results], list(range(1, 7)))
        self.assertTrue(
            any(
                warning.startswith("search_engine_diversity_collapsed:")
                for warning in provider.last_warnings
            ),
            provider.last_warnings,
        )
        self.assertIn("duckduckgo", provider.engine_health())

    async def test_cooled_engine_is_excluded_from_pinning(self):
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.params.get("engines"):
                return httpx.Response(
                    200,
                    json={"results": [{"url": "https://mwmbl.example/one", "engines": ["mwmbl"]}]},
                )
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"url": f"https://cse.example/{index}", "engines": ["google cse"]}
                        for index in range(3)
                    ],
                    "unresponsive_engines": [["duckduckgo", "CAPTCHA"]],
                },
            )

        provider = SearXNGSearchProvider(
            "http://searxng.test",
            retry_base_seconds=0,
            healthy_engines="duckduckgo,mwmbl",
        )
        await provider._client.aclose()
        provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            await provider.search("context engineering")
        finally:
            await provider.close()

        self.assertEqual(requests[1].url.params["engines"], "mwmbl")

    async def test_missing_engine_attribution_is_not_reported_as_collapse(self):
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "results": [{"url": f"https://example.com/{index}"} for index in range(5)],
                    "unresponsive_engines": [["google", "captcha"]],
                },
            )

        provider = SearXNGSearchProvider(
            "http://searxng.test", retry_base_seconds=0, healthy_engines="mwmbl"
        )
        await provider._client.aclose()
        provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            results = await provider.search("example")
        finally:
            await provider.close()

        self.assertEqual(len(requests), 1)
        self.assertEqual(len(results), 5)
        self.assertEqual(provider.last_warnings, ["search_engines_unresponsive:google: captcha"])

    async def test_engine_cooldowns_survive_a_restart(self) -> None:
        """A restart must not look healthy simply because memory was cleared."""
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "research.sqlite3")
            store.record_engine_cooldown("brave", "CAPTCHA challenge", time.time() + 1200)
            provider = SearXNGSearchProvider(
                "http://searxng.test",
                store=store,
                healthy_engines="brave,wiby",
            )
            try:
                health = provider.engine_health()
                self.assertIn("brave", health)
                self.assertIn("restored", health["brave"])
                self.assertEqual(provider._available_engines(), ["wiby"])
            finally:
                await provider.close()
                store.close()

    async def test_penalties_persist_as_wall_clock_not_monotonic(self) -> None:
        """Monotonic expiries are meaningless in the next process, so this must be wall clock."""
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "research.sqlite3")
            provider = SearXNGSearchProvider(
                "http://searxng.test",
                store=store,
                healthy_engines="brave",
            )
            try:
                provider._cool("brave", "CAPTCHA challenge")
                active = store.active_engine_cooldowns()
                self.assertIn("brave", active)
                # A persisted monotonic value would land nowhere near the 1800s CAPTCHA window.
                self.assertGreater(active["brave"][1], 1700)
                self.assertLessEqual(active["brave"][1], 1800)
            finally:
                await provider.close()
                store.close()


if __name__ == "__main__":
    unittest.main()
