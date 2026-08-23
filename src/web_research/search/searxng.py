from __future__ import annotations

import hashlib
import json
from typing import Any

from ..models import SearchResult
from ..safety.urls import canonicalize_url
from ..storage import SQLiteStore


class SearXNGError(RuntimeError):
    pass


class SearXNGSearchProvider:
    def __init__(
        self,
        base_url: str,
        *,
        store: SQLiteStore | None = None,
        cache_ttl_seconds: int = 900,
        timeout_seconds: float = 20.0,
        user_agent: str = "LocalResearchBot/0.1",
    ) -> None:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - dependency error is user-facing
            raise RuntimeError("httpx is required; install the project dependencies") from exc
        self.base_url = base_url.rstrip("/")
        self.store = store
        self.cache_ttl_seconds = cache_ttl_seconds
        self._client = httpx.AsyncClient(
            timeout=timeout_seconds,
            headers={"User-Agent": user_agent, "Accept": "application/json"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def search(
        self,
        query: str,
        *,
        page: int = 1,
        language: str | None = None,
        time_range: str | None = None,
        limit: int = 10,
    ) -> list[SearchResult]:
        cache_key = _cache_key(query, page, language, time_range, limit)
        if self.store:
            cached = self.store.get_search(cache_key, self.cache_ttl_seconds)
            if cached is not None:
                return cached

        params: dict[str, str | int] = {
            "q": query,
            "format": "json",
            "pageno": page,
        }
        if language:
            params["language"] = language
        normalized_range = _normalize_time_range(time_range)
        if normalized_range:
            params["time_range"] = normalized_range

        try:
            response = await self._client.get(f"{self.base_url}/search", params=params)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            raise SearXNGError(f"SearXNG request failed: {exc}") from exc

        raw_results = payload.get("results")
        if not isinstance(raw_results, list):
            raise SearXNGError("SearXNG returned no JSON results array")

        results: list[SearchResult] = []
        seen: set[str] = set()
        for rank, item in enumerate(raw_results, start=1):
            if not isinstance(item, dict) or not item.get("url"):
                continue
            try:
                canonical = canonicalize_url(str(item["url"]))
            except (TypeError, ValueError):
                continue
            if canonical in seen:
                continue
            seen.add(canonical)
            engines_value = item.get("engines") or item.get("engine") or []
            if isinstance(engines_value, str):
                engines = [engines_value]
            elif isinstance(engines_value, list):
                engines = [str(value) for value in engines_value]
            else:
                engines = []
            results.append(
                SearchResult(
                    url=canonical,
                    title=str(item.get("title") or canonical).strip(),
                    snippet=str(item.get("content") or "").strip(),
                    engines=engines,
                    published_at=_published_at(item),
                    rank=rank,
                    score=float(item.get("score") or 0.0),
                )
            )
            if len(results) >= limit:
                break

        # Empty result sets are often transient when upstream engines are rate-limited or
        # challenged. Do not poison the cache with a temporary aggregate failure.
        if self.store and results:
            self.store.put_search(cache_key, results)
        return results


def _cache_key(
    query: str, page: int, language: str | None, time_range: str | None, limit: int
) -> str:
    value = json.dumps(
        [query.strip(), page, language or "", time_range or "", limit], ensure_ascii=False
    )
    return hashlib.sha256(value.encode()).hexdigest()


def _normalize_time_range(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip().lower()
    return normalized if normalized in {"day", "month", "year"} else None


def _published_at(item: dict[str, Any]) -> str | None:
    for key in ("publishedDate", "published_at", "date"):
        value = item.get(key)
        if value:
            return str(value)
    return None
