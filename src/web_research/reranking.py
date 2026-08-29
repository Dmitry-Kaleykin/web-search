from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Protocol

from .models import SearchResult


class RerankingError(RuntimeError):
    pass


class CandidateReranker(Protocol):
    model: str

    async def rerank(self, query: str, candidates: list[SearchResult]) -> dict[str, float]: ...

    def usage(self) -> RerankerUsage: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class RerankerUsage:
    requests: int
    candidates: int
    failures: int
    disabled: bool


class OpenAICompatibleReranker:
    """Cohere/Jina-style reranker implemented by oMLX at POST /v1/rerank."""

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: str = "",
        timeout_seconds: float = 30.0,
        max_candidates: int = 24,
        disable_after_failures: int = 2,
    ) -> None:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - dependency error is user-facing
            raise RuntimeError("httpx is required; install the project dependencies") from exc
        if not model.strip():
            raise ValueError("reranker model must not be empty")
        self.base_url = base_url.rstrip("/")
        self.model = model.strip()
        self.max_candidates = max(1, max_candidates)
        self.disable_after_failures = max(1, disable_after_failures)
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.AsyncClient(timeout=timeout_seconds, headers=headers)
        self.requests = 0
        self.candidates = 0
        self.failures = 0
        self.disabled = False

    async def close(self) -> None:
        await self._client.aclose()

    def usage(self) -> RerankerUsage:
        return RerankerUsage(
            requests=self.requests,
            candidates=self.candidates,
            failures=self.failures,
            disabled=self.disabled,
        )

    async def rerank(self, query: str, candidates: list[SearchResult]) -> dict[str, float]:
        if self.disabled or not candidates:
            return {}
        bounded = candidates[: self.max_candidates]
        self.requests += 1
        self.candidates += len(bounded)
        try:
            response = await self._client.post(
                f"{self.base_url}/rerank",
                json={
                    "model": self.model,
                    "query": query,
                    "documents": [_candidate_text(item) for item in bounded],
                    "top_n": len(bounded),
                    "return_documents": False,
                },
            )
            response.raise_for_status()
            return _parse_scores(response.json(), bounded)
        except Exception as exc:
            self.failures += 1
            if self.failures >= self.disable_after_failures:
                self.disabled = True
            if isinstance(exc, RerankingError):
                raise
            raise RerankingError(f"Reranker request failed: {type(exc).__name__}: {exc}") from exc


def _candidate_text(candidate: SearchResult) -> str:
    return f"Title: {candidate.title}\nSnippet: {candidate.snippet}"[:8_000]


def _parse_scores(payload: Any, candidates: list[SearchResult]) -> dict[str, float]:
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise RerankingError("Reranker returned no results array")
    raw_results = payload["results"]
    if len(raw_results) != len(candidates):
        raise RerankingError("Reranker did not score every candidate")
    scores: dict[str, float] = {}
    seen_indexes: set[int] = set()
    for raw in raw_results:
        if not isinstance(raw, dict):
            raise RerankingError("Reranker returned an invalid result")
        index = raw.get("index")
        score = raw.get("relevance_score")
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or index < 0
            or index >= len(candidates)
            or index in seen_indexes
            or not isinstance(score, (int, float))
            or isinstance(score, bool)
            or not math.isfinite(score)
            or score < 0
            or score > 1
        ):
            raise RerankingError("Reranker returned invalid candidate scores")
        seen_indexes.add(index)
        scores[candidates[index].url] = float(score)
    return scores
