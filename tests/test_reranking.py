from __future__ import annotations

import unittest

import httpx

from web_research.models import SearchResult
from web_research.reranking import OpenAICompatibleReranker, RerankingError


class RerankerTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_rerank_contract_and_score_mapping(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"index": 1, "relevance_score": 0.2},
                        {"index": 0, "relevance_score": 0.9},
                    ]
                },
            )

        reranker = OpenAICompatibleReranker(
            "http://localhost:8000/v1", "Qwen3-Reranker-0.6B-mxfp8", api_key="token"
        )
        await reranker._client.aclose()
        reranker._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            headers={"Authorization": "Bearer token"},
        )
        candidates = [
            SearchResult(url="https://a.example", title="Relevant", snippet="Authentication"),
            SearchResult(url="https://b.example", title="Other", snippet="Colors"),
        ]
        try:
            scores = await reranker.rerank("authentication", candidates)
        finally:
            await reranker.close()

        self.assertEqual(scores, {"https://a.example": 0.9, "https://b.example": 0.2})
        self.assertEqual(requests[0].url.path, "/v1/rerank")
        self.assertEqual(requests[0].headers["authorization"], "Bearer token")
        payload = __import__("json").loads(requests[0].content)
        self.assertEqual(payload["model"], "Qwen3-Reranker-0.6B-mxfp8")
        self.assertEqual(payload["top_n"], 2)
        self.assertEqual(reranker.usage().requests, 1)
        self.assertEqual(reranker.usage().candidates, 2)

    async def test_invalid_responses_open_per_run_circuit(self) -> None:
        reranker = OpenAICompatibleReranker(
            "http://localhost:8000/v1", "reranker", disable_after_failures=1
        )
        await reranker._client.aclose()
        reranker._client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200, json={"results": [{"index": 0, "relevance_score": 0.8}]}
                )
            )
        )
        candidates = [
            SearchResult(url="https://a.example", title="A"),
            SearchResult(url="https://b.example", title="B"),
        ]
        try:
            with self.assertRaises(RerankingError):
                await reranker.rerank("query", candidates)
            self.assertTrue(reranker.usage().disabled)
            self.assertEqual(await reranker.rerank("query", candidates), {})
        finally:
            await reranker.close()


if __name__ == "__main__":
    unittest.main()
