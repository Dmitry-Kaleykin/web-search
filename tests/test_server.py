from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from mcp.client import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import ClientCapabilities, SamplingCapability

from web_research.config import Settings
from web_research.model.fallback import FallbackModelClient
from web_research.model.mcp_sampling import MCPSamplingModelClient
from web_research.model.openai_compatible import OpenAICompatibleModelClient
from web_research.model.unavailable import UnavailableModelClient
from web_research.models import Document
from web_research.server import (
    ConcurrentResearchError,
    _create_evidence_model,
    _create_model,
    _create_reader_runtime,
    _read_url_output,
    _SingleFlight,
    mcp,
    read_url,
)


class MCPServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_flight_rejects_overlapping_research(self) -> None:
        gate = _SingleFlight()
        gate.start()

        with self.assertRaisesRegex(ConcurrentResearchError, "already running"):
            gate.start()

        gate.finish()
        gate.start()
        gate.finish()

    async def test_stdio_entry_negotiates_sampling_compatible_handshake(self) -> None:
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
        self.assertIn("answer_markdown", search_tool.output_schema["properties"])
        self.assertNotIn(
            "has_primary",
            search_tool.output_schema["$defs"]["ToolCoverageItem"]["properties"],
        )
        stats_schema = search_tool.output_schema["$defs"]["ToolStats"]["properties"]
        self.assertIn("evidence_model", stats_schema)
        self.assertIn("evidence_model_successes", stats_schema)
        self.assertIn("evidence_model_fallbacks", stats_schema)
        self.assertIn("reranker_requests", stats_schema)
        self.assertIn("candidates_rejected_irrelevant", stats_schema)
        self.assertIn("relevance_batches_rejected", stats_schema)
        self.assertIn("prefetch_started", stats_schema)
        self.assertIn("Never add a calendar year", search_tool.description)
        self.assertIn("do not invoke web_search in parallel", search_tool.description)
        self.assertIn(
            "do not add a year",
            search_tool.input_schema["properties"]["query"]["description"],
        )
        freshness_schema = search_tool.input_schema["properties"]["freshness"]
        self.assertIn("Do not resolve relative wording", freshness_schema["description"])

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
        runtime = SimpleNamespace(reader=reader, close=AsyncMock())
        context = SimpleNamespace(report_progress=AsyncMock())
        settings = Settings(read_url_max_chars=100, read_url_max_links=10)

        with (
            patch("web_research.server.Settings.from_env", return_value=settings),
            patch("web_research.server._create_reader_runtime", return_value=runtime),
        ):
            output = await read_url(
                "https://example.com/page",
                context,
                render="always",
            )

        reader.read.assert_awaited_once_with(
            "https://example.com/page",
            render="always",
        )
        runtime.close.assert_awaited_once()
        self.assertEqual(output.extraction_method, "crawl4ai+chromium")
        self.assertEqual(output.content, "Extracted content")

    async def test_dynamic_client_model_takes_precedence_over_direct_fallback(self) -> None:
        context = SimpleNamespace(
            client_capabilities=ClientCapabilities(sampling=SamplingCapability())
        )
        model = _create_model(context, Settings(model_id="fallback-model"))

        self.assertIsInstance(model, MCPSamplingModelClient)
        self.assertEqual(model.timeout_seconds, 90.0)
        await model.close()

    async def test_direct_model_is_used_when_sampling_is_unavailable(self) -> None:
        context = SimpleNamespace(client_capabilities=ClientCapabilities())
        model = _create_model(context, Settings(model_id="fallback-model"))

        self.assertIsInstance(model, OpenAICompatibleModelClient)
        await model.close()

    async def test_missing_sampling_and_fallback_uses_deterministic_mode(self) -> None:
        context = SimpleNamespace(client_capabilities=ClientCapabilities())
        model = _create_model(context, Settings())

        self.assertIsInstance(model, UnavailableModelClient)
        await model.close()

    async def test_dedicated_evidence_model_wraps_dynamic_model_with_fallback(self) -> None:
        dynamic = UnavailableModelClient()
        settings = Settings(
            evidence_model_base_url="http://reader.test/v1",
            evidence_model_id="reader-model",
            evidence_model_max_tokens=1234,
        )

        model = _create_evidence_model(settings, dynamic)

        self.assertIsInstance(model, FallbackModelClient)
        self.assertIs(model.fallback, dynamic)
        self.assertIsInstance(model.preferred, OpenAICompatibleModelClient)
        self.assertEqual(model.preferred.model, "reader-model")
        self.assertEqual(model.preferred.max_tokens, 1234)
        await model.close()

    async def test_no_dedicated_evidence_model_reuses_dynamic_model(self) -> None:
        dynamic = UnavailableModelClient()

        model = _create_evidence_model(Settings(), dynamic)

        self.assertIs(model, dynamic)


if __name__ == "__main__":
    unittest.main()
