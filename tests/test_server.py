from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace

from mcp.client import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import ClientCapabilities, SamplingCapability

from web_research.config import Settings
from web_research.model.fallback import FallbackModelClient
from web_research.model.mcp_sampling import MCPSamplingModelClient
from web_research.model.openai_compatible import OpenAICompatibleModelClient
from web_research.model.unavailable import UnavailableModelClient
from web_research.server import (
    ConcurrentResearchError,
    _create_evidence_model,
    _create_model,
    _SingleFlight,
    mcp,
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
        self.assertEqual([tool.name for tool in result.tools], ["web_search"])

    async def test_server_exposes_exactly_one_structured_tool(self) -> None:
        async with Client(mcp) as client:
            result = await client.list_tools()
        self.assertEqual([tool.name for tool in result.tools], ["web_search"])
        tool = result.tools[0]
        self.assertEqual(tool.input_schema["required"], ["query"])
        self.assertIn("answer_markdown", tool.output_schema["properties"])
        self.assertNotIn(
            "has_primary",
            tool.output_schema["$defs"]["ToolCoverageItem"]["properties"],
        )
        self.assertIn("Never add a calendar year", tool.description)
        self.assertIn("do not invoke web_search in parallel", tool.description)
        self.assertIn("do not add a year", tool.input_schema["properties"]["query"]["description"])
        freshness_schema = tool.input_schema["properties"]["freshness"]
        self.assertIn("Do not resolve relative wording", freshness_schema["description"])

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
