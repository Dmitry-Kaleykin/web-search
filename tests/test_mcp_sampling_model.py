from __future__ import annotations

import asyncio
import unittest
import warnings
from types import SimpleNamespace
from unittest.mock import AsyncMock

from mcp import MCPDeprecationWarning
from mcp.client import Client
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import ClientCapabilities, CreateMessageResult, SamplingCapability, TextContent

from web_research.model.mcp_sampling import MCPSamplingModelClient
from web_research.model.openai_compatible import ModelError


def sampling_context(result: CreateMessageResult | None = None):
    session = SimpleNamespace(
        create_message=AsyncMock(
            return_value=result
            or CreateMessageResult(
                role="assistant",
                content=TextContent(text='```json\n{"answer": "dynamic"}\n```'),
                model="pi/current-model",
                stopReason="endTurn",
            )
        )
    )
    return SimpleNamespace(
        client_capabilities=ClientCapabilities(sampling=SamplingCapability()),
        session=session,
        request_id="request-1",
    )


class MCPSamplingModelClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_sampling_round_trip_over_pi_compatible_protocol(self):
        server = MCPServer("sampling-wire-test")

        @server.tool()
        async def ask(ctx: Context) -> str:
            result = await MCPSamplingModelClient(ctx).complete_json(
                system="Return JSON.",
                user="Question",
                schema_name="test",
                schema={"type": "object"},
            )
            return str(result["answer"])

        async def sample(_context, params):
            self.assertEqual(params.messages[0].content.text, "Question")
            self.assertEqual(params.include_context, "none")
            return CreateMessageResult(
                role="assistant",
                content=TextContent(text='{"answer":"wire-ok"}'),
                model="pi/current-model",
            )

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", MCPDeprecationWarning)
            async with Client(
                server,
                mode="legacy",
                sampling_callback=sample,
                sampling_capabilities=SamplingCapability(),
            ) as client:
                result = await client.call_tool("ask")

        self.assertFalse(result.is_error)
        self.assertEqual(result.content[0].text, "wire-ok")

    async def test_uses_client_sampling_and_parses_json(self):
        context = sampling_context()
        client = MCPSamplingModelClient(context, max_tokens=321, temperature=0.2)

        result = await client.complete_json(
            system="Research safely.",
            user="Question",
            schema_name="answer",
            schema={"type": "object"},
        )

        self.assertEqual(result, {"answer": "dynamic"})
        call = context.session.create_message.await_args
        self.assertEqual(call.kwargs["max_tokens"], 321)
        self.assertEqual(call.kwargs["temperature"], 0.2)
        self.assertEqual(call.kwargs["include_context"], "none")
        self.assertEqual(call.kwargs["related_request_id"], "request-1")
        self.assertNotIn("model_preferences", call.kwargs)
        self.assertIn("JSON Schema", call.kwargs["system_prompt"])
        self.assertEqual(call.kwargs["messages"][0].content.text, "Question")

    async def test_rejects_context_without_sampling_capability(self):
        context = SimpleNamespace(
            client_capabilities=ClientCapabilities(),
            session=SimpleNamespace(create_message=AsyncMock()),
            request_id="request-1",
        )
        client = MCPSamplingModelClient(context)

        with self.assertRaisesRegex(ModelError, "does not support model sampling"):
            await client.complete_json(
                system="System",
                user="Question",
                schema_name="test",
                schema={"type": "object"},
            )
        context.session.create_message.assert_not_awaited()

    async def test_rejects_non_text_sampling_result(self):
        context = sampling_context(
            CreateMessageResult(
                role="assistant",
                content=TextContent(text="not json"),
                model="pi/current-model",
            )
        )
        client = MCPSamplingModelClient(context)

        with self.assertRaisesRegex(ModelError, "invalid JSON"):
            await client.complete_json(
                system="System",
                user="Question",
                schema_name="test",
                schema={"type": "object"},
            )

    async def test_sampling_has_an_independent_model_timeout(self):
        context = sampling_context()

        async def slow_sampling(**_kwargs):
            await asyncio.sleep(1)

        context.session.create_message = AsyncMock(side_effect=slow_sampling)
        client = MCPSamplingModelClient(context, timeout_seconds=0.01)

        with self.assertRaisesRegex(ModelError, "sampling failed"):
            await client.complete_json(
                system="System",
                user="Question",
                schema_name="test",
                schema={"type": "object"},
            )

        self.assertEqual(context.session.create_message.await_count, 1)


if __name__ == "__main__":
    unittest.main()
