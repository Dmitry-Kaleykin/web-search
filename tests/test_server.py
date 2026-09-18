from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from mcp.client import Client
from mcp.client.stdio import StdioServerParameters, stdio_client

from web_research.config import Settings
from web_research.models import Document
from web_research.server import (
    _create_reader_runtime,
    _read_url_output,
    mcp,
    read_url,
    web_search,
)
from web_research.storage import SQLiteStore


class MCPServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_entry_negotiates_existing_handshake(self) -> None:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "web_research.server"],
        )
        async with Client(stdio_client(params), mode="auto") as client:
            result = await client.list_tools()
            protocol_version = client.session.protocol_version

        self.assertEqual(protocol_version, "2025-11-25")
        self.assertEqual([tool.name for tool in result.tools], ["read_url", "web_search"])

    async def test_server_exposes_structured_search_and_url_reader_tools(self) -> None:
        async with Client(mcp) as client:
            result = await client.list_tools()
        self.assertEqual([tool.name for tool in result.tools], ["read_url", "web_search"])
        tools = {tool.name: tool for tool in result.tools}
        read_tool = tools["read_url"]
        search_tool = tools["web_search"]

        self.assertEqual(read_tool.input_schema["required"], ["url"])
        self.assertEqual(
            read_tool.input_schema["properties"]["render"]["enum"],
            ["auto", "never", "always"],
        )
        self.assertIn("content", read_tool.output_schema["properties"])
        self.assertIn("content_truncated", read_tool.output_schema["properties"])
        self.assertIn("status_code", read_tool.output_schema["properties"])
        self.assertIn("page_status", read_tool.output_schema["properties"])
        self.assertIn("next_cursor", read_tool.output_schema["properties"])
        self.assertEqual(read_tool.input_schema["properties"]["max_chars"]["default"], 4_000)
        self.assertFalse(read_tool.input_schema["properties"]["include_links"]["default"])
        self.assertIn("instead of curl or wget", read_tool.description)

        self.assertEqual(search_tool.input_schema["required"], ["query"])
        self.assertIn("results", search_tool.output_schema["properties"])
        self.assertIn("engine_health", search_tool.output_schema["properties"])
        self.assertIn("cache_hit", search_tool.output_schema["properties"])
        self.assertIn("retrieved_at", search_tool.output_schema["properties"])
        self.assertNotIn("answer_markdown", search_tool.output_schema["properties"])
        self.assertNotIn("coverage", search_tool.output_schema["properties"])
        self.assertNotIn("effort", search_tool.input_schema["properties"])
        self.assertNotIn("freshness", search_tool.input_schema["properties"])
        self.assertIn("does not read pages", search_tool.description)
        self.assertIn("read_url", search_tool.description)

    async def test_shared_reader_runtime_can_disable_browser(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = _create_reader_runtime(
                Settings(data_dir=Path(directory), enable_crawl4ai=False)
            )
            try:
                self.assertIsNone(runtime.reader.browser)
                self.assertTrue((Path(directory) / "research.sqlite3").exists())
            finally:
                await runtime.close()

    async def test_read_url_output_is_bounded_and_reports_original_sizes(self) -> None:
        document = Document(
            url="https://example.com/page",
            final_url="https://example.com/page",
            title="Example",
            content="abcdefghij",
            method="http+trafilatura",
            links=["https://example.com/a", "https://example.com/b"],
        )

        output = _read_url_output(
            document,
            Settings(read_url_max_chars=5, read_url_max_links=1),
            include_links=True,
        )

        self.assertEqual(output.content, "abcde")
        self.assertEqual(output.content_characters, 10)
        self.assertTrue(output.content_truncated)
        self.assertEqual(output.links, ["https://example.com/a"])
        self.assertEqual(output.link_count, 2)
        self.assertTrue(output.links_truncated)
        self.assertIn("tool_output_truncated:content", output.warnings)
        self.assertIn("tool_output_truncated:links", output.warnings)

    async def test_read_url_output_supports_cached_content_pagination(self) -> None:
        document = Document(
            url="https://example.com/page",
            final_url="https://example.com/page",
            title="Example",
            content="abcdefghij",
            method="http+trafilatura",
            status_code=200,
            links=["https://example.com/a"],
        )

        output = _read_url_output(
            document,
            Settings(read_url_max_chars=20, read_url_max_links=10),
            cursor=4,
            max_chars=3,
            include_links=False,
        )

        self.assertEqual(output.content, "efg")
        self.assertEqual(output.content_start, 4)
        self.assertEqual(output.content_end, 7)
        self.assertTrue(output.has_more_content)
        self.assertEqual(output.next_cursor, 7)
        self.assertEqual(output.status_code, 200)
        self.assertEqual(output.page_status, "ok")
        self.assertFalse(output.links_included)
        self.assertEqual(output.links, [])

    async def test_read_url_output_can_focus_past_navigation(self) -> None:
        document = Document(
            url="https://example.com/page",
            final_url="https://example.com/page",
            title="Example",
            content=(
                "Navigation products docs account\n\n" * 200
                + "# Browser compatibility\n\nFirefox supports anchor-size from version 147."
            ),
            method="crawl4ai+chromium",
        )

        output = _read_url_output(
            document,
            Settings(read_url_max_chars=500),
            max_chars=500,
            query="anchor-size Firefox browser compatibility",
        )

        self.assertGreater(output.content_start, 0)
        self.assertIn("Browser compatibility", output.content)
        self.assertIn("tool_output_relevance_window", output.warnings)

    async def test_read_url_output_surfaces_suspected_error_page(self) -> None:
        document = Document(
            url="https://example.com/error",
            final_url="https://example.com/error",
            title="Example | 525: SSL handshake failed",
            content="Cloudflare origin error",
            method="http+trafilatura",
            status_code=200,
        )

        output = _read_url_output(document, Settings())

        self.assertEqual(output.status_code, 200)
        self.assertEqual(output.page_status, "suspected_error")
        self.assertIn("suspected_error_page:cloudflare_525", output.warnings)

    async def test_read_url_uses_shared_reader_and_requested_render_mode(self) -> None:
        document = Document(
            url="https://example.com/page",
            final_url="https://example.com/page",
            title="Example",
            content="Extracted content",
            method="crawl4ai+chromium",
        )
        reader = SimpleNamespace(read=AsyncMock(return_value=document))
        runtime = SimpleNamespace(
            reader=reader,
            close=AsyncMock(),
            store=SimpleNamespace(put_snapshot=Mock(return_value="a" * 32)),
        )
        context = SimpleNamespace(report_progress=AsyncMock())
        settings = Settings(read_url_max_chars=100, read_url_max_links=10)

        with (
            patch("web_research.server.Settings.from_env", return_value=settings),
            patch("web_research.server._shared_reader_runtime", return_value=runtime),
        ):
            output = await read_url(
                "https://example.com/page",
                context,
                render="always",
            )

        reader.read.assert_awaited_once_with(
            "https://example.com/page",
            render="always",
            query=None,
            max_age_seconds=None,
            visual=False,
            page=1,
            actions=None,
        )
        runtime.close.assert_not_awaited()
        self.assertEqual(output.extraction_method, "crawl4ai+chromium")
        self.assertEqual(output.content, "Extracted content")

    @contextmanager
    def search_fixture(self, *, payload=None, timeout=30.0):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(
                data_dir=Path(directory),
                search_healthy_engines="brave,google cse",
                search_timeout_seconds=timeout,
                model_id="unused-model",
                reranker_model_id="unused-reranker",
            )
            store = SQLiteStore(Path(directory) / "test.sqlite3")
            runtime = SimpleNamespace(store=store, reader=SimpleNamespace(read=AsyncMock()))
            try:
                with (
                    patch("web_research.server.Settings.from_env", return_value=settings),
                    patch("web_research.server._shared_reader_runtime", return_value=runtime),
                    patch(
                        "web_research.search.searxng.SearXNGSearchProvider._request",
                        new_callable=AsyncMock,
                    ) as request,
                ):
                    request.return_value = payload or {"results": [], "failures": []}
                    yield store, runtime, request
            finally:
                store.close()

    async def test_search_returns_raw_discovery_without_reading_or_sampling(self):
        payload = {
            "results": [
                {
                    "url": "https://example.com/phone",
                    "title": "Phone",
                    "content": "A snippet",
                    "engines": ["brave"],
                    "publishedDate": "2026-09-17",
                }
            ],
            "failures": [],
        }
        # This client has no sampling capability or model interface.
        async with Client(mcp) as client:
            with self.search_fixture(payload=payload) as (store, runtime, request):
                result = await client.call_tool(
                    "web_search",
                    {
                        "query": "складной телефон",
                        "language": "ru",
                        "page": 2,
                        "limit": 5,
                        "time_range": "month",
                        "refresh": True,
                    },
                )
                self.assertFalse(result.is_error)
                output = result.structured_content
                self.assertEqual(output["outcome"], "success")
                self.assertEqual(output["results"][0]["snippet"], "A snippet")
                self.assertEqual(output["results"][0]["url"], "https://example.com/phone")
                self.assertTrue(output["results"][0]["published_at"].startswith("2026-09-17"))
                request.assert_awaited_once_with(
                    {
                        "q": "складной телефон",
                        "format": "json",
                        "pageno": 2,
                        "language": "ru",
                        "time_range": "month",
                        "engines": "brave,google cse",
                    }
                )
                runtime.reader.read.assert_not_awaited()
                self.assertEqual(
                    store._connection.execute("SELECT COUNT(*) FROM research_runs").fetchone()[0], 0
                )

    async def test_search_preserves_results_and_reports_degraded_engines(self):
        payload = {
            "results": [{"url": "https://example.com/a"}],
            "failures": [["brave", "too many requests"]],
        }
        with self.search_fixture(payload=payload) as (_, _, request):
            result = await web_search("test", SimpleNamespace(report_progress=AsyncMock()))
        self.assertEqual(result.outcome, "success")
        self.assertEqual(len(result.results), 1)
        self.assertIn("brave", result.engine_health)
        self.assertTrue(any("unresponsive" in w for w in result.warnings))
        request.assert_awaited_once()

    async def test_search_distinguishes_empty_from_unavailable_without_retrying(self):
        ctx = SimpleNamespace(report_progress=AsyncMock())
        with self.search_fixture() as (_, _, request):
            empty = await web_search("nothing", ctx)
            request.return_value = {"results": [], "failures": [["brave", "timeout"]]}
            unavailable = await web_search("failed", ctx)
            self.assertEqual(request.await_count, 2)
        self.assertEqual(empty.outcome, "empty")
        self.assertEqual(unavailable.outcome, "backend_unavailable")
        self.assertTrue(any("search_failed" in w for w in unavailable.warnings))

    async def test_search_reloads_cooldowns_before_next_dispatch(self):
        import time

        with self.search_fixture() as (store, _, request):
            store.record_engine_cooldown("brave", "too many requests", time.time() + 900)
            result = await web_search("test", SimpleNamespace(report_progress=AsyncMock()))
            self.assertEqual(request.call_args.args[0]["engines"], "google cse")
            store.record_engine_cooldown("google cse", "access denied", time.time() + 900)
            blocked = await web_search("test again", SimpleNamespace(report_progress=AsyncMock()))
            request.assert_awaited_once()
        self.assertIn("brave", result.engine_health)
        self.assertEqual(blocked.outcome, "backend_unavailable")

    async def test_search_cache_and_refresh(self):
        payload = {"results": [{"url": "https://example.com/a"}], "failures": []}
        ctx = SimpleNamespace(report_progress=AsyncMock())
        with self.search_fixture(payload=payload) as (_, _, request):
            first = await web_search("same", ctx)
            cached = await web_search("same", ctx)
            fresh = await web_search("same", ctx, refresh=True)
            self.assertEqual(request.await_count, 2)
        self.assertFalse(first.cache_hit)
        self.assertTrue(cached.cache_hit)
        self.assertFalse(fresh.cache_hit)

    async def test_parallel_searches_queue_and_observe_previous_failure(self):
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        calls = []

        async def respond(params):
            calls.append(params)
            if len(calls) == 1:
                first_started.set()
                await release_first.wait()
                return {"results": [], "failures": [["brave", "too many requests"]]}
            return {"results": [], "failures": []}

        ctx = SimpleNamespace(report_progress=AsyncMock())
        with self.search_fixture() as (_, _, request):
            request.side_effect = respond
            first = asyncio.create_task(web_search("one", ctx))
            await asyncio.wait_for(first_started.wait(), 1)
            second = asyncio.create_task(web_search("two", ctx))
            await asyncio.sleep(0)
            self.assertEqual(len(calls), 1)
            release_first.set()
            outputs = await asyncio.wait_for(asyncio.gather(first, second), 1)
        self.assertEqual(calls[1]["engines"], "google cse")
        self.assertEqual(outputs[0].outcome, "backend_unavailable")
        self.assertEqual(outputs[1].outcome, "empty")

    async def test_endpoint_cooldown_prevents_network_but_allows_cached_results(self):
        import time

        ctx = SimpleNamespace(report_progress=AsyncMock())
        payload = {"results": [{"url": "https://example.com/a"}], "failures": []}
        with self.search_fixture(payload=payload) as (store, _, request):
            await web_search("cached", ctx)
            store.record_engine_cooldown("searxng", "HTTP 429", time.time() + 900)
            cached = await web_search("cached", ctx)
            blocked = await web_search("cached", ctx, refresh=True)
            request.assert_awaited_once()
        self.assertTrue(cached.cache_hit)
        self.assertEqual(cached.outcome, "success")
        self.assertEqual(blocked.outcome, "backend_unavailable")
        self.assertIn("searxng", blocked.engine_health)

    async def test_cached_search_keeps_failure_provenance_after_engines_recover(self):
        import time

        ctx = SimpleNamespace(report_progress=AsyncMock())
        payload = {
            "results": [{"url": "https://example.com/a"}],
            "failures": [["brave", "timeout"]],
        }
        with self.search_fixture(payload=payload) as (store, _, request):
            first = await web_search("partial", ctx)
            with store._connection:
                store._connection.execute(
                    "UPDATE engine_health SET expires_at = ?", (time.time() - 1,)
                )
            cached = await web_search("partial", ctx)
            request.assert_awaited_once()
            request.return_value = {"results": [{"url": "https://example.com/new"}], "failures": []}
            fresh = await web_search("partial", ctx, refresh=True)
        self.assertTrue(cached.cache_hit)
        self.assertEqual(cached.engine_health, {})
        self.assertEqual(cached.warnings, first.warnings)
        self.assertTrue(any("brave: timeout" in warning for warning in cached.warnings))
        self.assertIsNotNone(first.retrieved_at)
        self.assertEqual(cached.retrieved_at, first.retrieved_at)
        self.assertGreater(cached.responded_at, cached.retrieved_at)
        self.assertGreater(fresh.retrieved_at, cached.retrieved_at)
        self.assertEqual(fresh.warnings, [])

    async def test_cached_search_remembers_engines_skipped_during_retrieval(self):
        import time

        ctx = SimpleNamespace(report_progress=AsyncMock())
        with self.search_fixture(
            payload={"results": [{"url": "https://example.com/a"}], "failures": []}
        ) as (store, _, request):
            store.record_engine_cooldown("brave", "timeout", time.time() + 60)
            first = await web_search("reduced pool", ctx)
            with store._connection:
                store._connection.execute("DELETE FROM engine_health")
            cached = await web_search("reduced pool", ctx)
            request.assert_awaited_once()
        self.assertIn("search_engines_skipped_at_retrieval:brave", first.warnings)
        self.assertEqual(cached.warnings, first.warnings)
        self.assertEqual(cached.engine_health, {})

    async def test_search_times_out_and_cancels_provider(self):
        cancelled = asyncio.Event()

        async def slow(_params):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with self.search_fixture(timeout=0.05) as (_, _, request):
            request.side_effect = slow
            result = await web_search("slow", SimpleNamespace(report_progress=AsyncMock()))
        self.assertTrue(cancelled.is_set())
        self.assertEqual(result.outcome, "backend_unavailable")
        self.assertTrue(any("search_timeout" in w for w in result.warnings))

    async def test_search_cancellation_releases_dispatch_gate(self):
        started = asyncio.Event()

        async def slow(_params):
            started.set()
            await asyncio.Event().wait()

        ctx = SimpleNamespace(report_progress=AsyncMock())
        with self.search_fixture() as (_, _, request):
            request.side_effect = slow
            task = asyncio.create_task(web_search("cancel", ctx))
            await asyncio.wait_for(started.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            request.side_effect = None
            result = await asyncio.wait_for(web_search("next", ctx), 1)
        self.assertEqual(result.outcome, "empty")

    async def test_search_validates_mcp_limits(self):
        async with Client(mcp) as client:
            for arguments in (
                {"query": "x", "limit": 21},
                {"query": "x", "page": 0},
                {"query": "x", "time_range": "week"},
                {"query": "   "},
            ):
                result = await client.call_tool("web_search", arguments)
                self.assertTrue(result.is_error)


if __name__ == "__main__":
    unittest.main()
