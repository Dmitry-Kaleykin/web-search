from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from ..dates import normalize_published_at
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
        max_retries: int = 2,
        retry_base_seconds: float = 0.25,
    ) -> None:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - dependency error is user-facing
            raise RuntimeError("httpx is required; install the project dependencies") from exc
        self.base_url = base_url.rstrip("/")
        self.store = store
        self.cache_ttl_seconds = cache_ttl_seconds
        self.max_retries = max(0, max_retries)
        self.retry_base_seconds = max(0.0, retry_base_seconds)
        self.last_warnings: list[str] = []
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
        categories: str | None = None,
        limit: int = 10,
    ) -> list[SearchResult]:
        self.last_warnings = []
        cache_key = _cache_key(query, page, language, time_range, categories, limit)
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
        if categories:
            params["categories"] = categories

        payload: dict[str, Any] = {}
        raw_results: list[Any] = []
        for attempt in range(self.max_retries + 1):
            try:
                response = await self._client.get(f"{self.base_url}/search", params=params)
                response.raise_for_status()
                decoded = response.json()
            except Exception as exc:
                if attempt < self.max_retries:
                    await _retry_delay(self.retry_base_seconds, attempt)
                    continue
                raise SearXNGError(
                    f"SearXNG request failed after {attempt + 1} attempt(s): {exc}"
                ) from exc
            if not isinstance(decoded, dict):
                raise SearXNGError("SearXNG returned a non-object JSON response")
            payload = decoded
            candidate_results = payload.get("results")
            if not isinstance(candidate_results, list):
                raise SearXNGError("SearXNG returned no JSON results array")
            raw_results = candidate_results
            engine_failures = _unresponsive_engines(payload)
            if engine_failures:
                warning = "search_engines_unresponsive:" + ", ".join(engine_failures)
                self.last_warnings = [warning]
            if raw_results or not engine_failures:
                break
            if attempt < self.max_retries:
                await _retry_delay(self.retry_base_seconds, attempt)
                continue
            raise SearXNGError(
                "SearXNG returned no results because upstream engines were unresponsive after "
                f"{attempt + 1} attempt(s): {', '.join(engine_failures)}"
            )

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


async def _retry_delay(base_seconds: float, attempt: int) -> None:
    if base_seconds:
        await asyncio.sleep(base_seconds * (2**attempt))


def _unresponsive_engines(payload: dict[str, Any]) -> list[str]:
    raw = payload.get("unresponsive_engines")
    if not isinstance(raw, list):
        return []
    failures: list[str] = []
    for item in raw[:12]:
        if isinstance(item, (list, tuple)):
            parts = [str(value).strip() for value in item[:2] if str(value).strip()]
            text = ": ".join(parts)
        elif isinstance(item, dict):
            engine = str(item.get("engine") or item.get("name") or "unknown").strip()
            error = str(item.get("error") or item.get("message") or "unresponsive").strip()
            text = f"{engine}: {error}"
        else:
            text = str(item).strip()
        if text and text not in failures:
            failures.append(text[:240])
    return failures


def _cache_key(
    query: str,
    page: int,
    language: str | None,
    time_range: str | None,
    categories: str | None,
    limit: int,
) -> str:
    value = json.dumps(
        [query.strip(), page, language or "", time_range or "", categories or "", limit],
        ensure_ascii=False,
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
            return normalize_published_at(value)
    return None
