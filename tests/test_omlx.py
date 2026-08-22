from __future__ import annotations

import json
import unittest

import httpx

from web_research.model.omlx import ModelError, OMLXModelClient


class OMLXModelClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_openai_compatible_chat_contract_and_fenced_json(self):
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": '```json\n{"answer": "ok"}\n```'}}]},
            )

        client = OMLXModelClient(
            "http://omlx.test/v1", "local-model", api_key="secret", max_tokens=321
        )
        headers = client._client.headers
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), headers=headers)
        try:
            result = await client.complete_json(
                system="Research safely.",
                user="Question",
                schema_name="answer",
                schema={"type": "object"},
            )
        finally:
            await client.close()

        self.assertEqual(result, {"answer": "ok"})
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].url.path, "/v1/chat/completions")
        self.assertEqual(requests[0].headers["authorization"], "Bearer secret")
        payload = json.loads(requests[0].content)
        self.assertEqual(payload["model"], "local-model")
        self.assertEqual(payload["max_tokens"], 321)
        self.assertIn("JSON Schema", payload["messages"][0]["content"])

    async def test_missing_model_is_reported_before_network_access(self):
        client = OMLXModelClient("http://omlx.test/v1", "")
        try:
            with self.assertRaisesRegex(ModelError, "not configured"):
                await client.complete_json(
                    system="System",
                    user="User",
                    schema_name="test",
                    schema={"type": "object"},
                )
        finally:
            await client.close()


if __name__ == "__main__":
    unittest.main()
