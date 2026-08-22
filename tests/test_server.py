from __future__ import annotations

import unittest
from types import SimpleNamespace

from mcp.client import Client
from mcp.types import ClientCapabilities, SamplingCapability

from web_research.config import Settings
from web_research.model.mcp_sampling import MCPSamplingModelClient
from web_research.model.openai_compatible import OpenAICompatibleModelClient
from web_research.model.unavailable import UnavailableModelClient
from web_research.server import _create_model, mcp


class MCPServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_server_exposes_exactly_one_structured_tool(self) -> None:
        async with Client(mcp) as client:
            result = await client.list_tools()
        self.assertEqual([tool.name for tool in result.tools], ["web_search"])
        tool = result.tools[0]
        self.assertEqual(tool.input_schema["required"], ["query"])
        self.assertIn("answer_markdown", tool.output_schema["properties"])

    async def test_dynamic_client_model_takes_precedence_over_direct_fallback(self) -> None:
        context = SimpleNamespace(
            client_capabilities=ClientCapabilities(sampling=SamplingCapability())
        )
        model = _create_model(context, Settings(model_id="fallback-model"))

        self.assertIsInstance(model, MCPSamplingModelClient)
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


if __name__ == "__main__":
    unittest.main()
