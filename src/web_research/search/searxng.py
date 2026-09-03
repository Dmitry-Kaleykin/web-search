"""SearXNG-backed search provider.

Beyond the plain JSON contract this layer tracks upstream engine health. SearXNG reports failing
engines in ``unresponsive_engines`` but still returns HTTP 200 with whatever survived, so a
single reachable index can answer every query while the rest of the fleet is blocked. Retrying the
identical request cannot fix that: a CAPTCHA wall and a rate-limit suspension both ignore fast
retries, and nested retries deepen the block.

This provider therefore (a) never retries an anti-bot challenge, (b) honours ``Retry-After``,
(c) puts failing engines on a cooldown, and (d) re-issues a collapsed query pinned to engines that
are actually answering.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
from dataclasses import dataclass
from typing import Any

from ..dates import normalize_published_at
from ..models import SearchResult
from ..safety.urls import canonicalize_url
from ..storage import SQLiteStore


class SearXNGError(RuntimeError):
    pass


class SearXNGChallengeError(SearXNGError):
    """The endpoint answered with an anti-bot page instead of JSON.

    Retrying is futile: proof-of-work and CAPTCHA challenges are answered by a browser, not by
    asking again. Repeated attempts against the same wall extend the block.
    """


class SearXNGRateLimitedError(SearXNGError):
    """Upstream refused on quota grounds and may have said when to come back."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


_CAPTCHA_MARKERS = ("captcha", "access denied", "unusual traffic", "blocked", "suspended")
_RATE_LIMIT_MARKERS = ("too many requests", "rate limit", "429", "ratelimit")
_TRANSIENT_MARKERS = ("timeout", "timed out", "crash", "connection", "reset", "temporarily", "eof")

_CHALLENGE_MARKERS = (
    "verifying your browser",
    "just a moment",
    "attention required",
    "cf-chl",
    "challenge-platform",
    "antibot",
    "pardon our interruption",
    "enable javascript and cookies",
)


@dataclass(slots=True)
class _EngineCooldown:
    reason: str
    expires: float


def _classify_failure(reason: str) -> float:
    """Seconds to leave an engine out of rotation, by how it failed."""
    text = reason.casefold()
    if any(marker in text for marker in _RATE_LIMIT_MARKERS):
        return 900.0
    if any(marker in text for marker in _CAPTCHA_MARKERS):
        return 1800.0
    if any(marker in text for marker in _TRANSIENT_MARKERS):
        return 120.0
    return 300.0


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
        retry_base_seconds: float = 1.0,
        healthy_engines: str = "",
        diversity_min_results: int = 3,
        max_retry_wait_seconds: float = 10.0,
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
        self.diversity_min_results = max(2, diversity_min_results)
        self.max_retry_wait_seconds = max(0.0, max_retry_wait_seconds)
        self.healthy_engines = _parse_engine_list(healthy_engines)
        self.last_warnings: list[str] = []
        self.last_engine_health: dict[str, str] = {}
        self._cooldowns: dict[str, _EngineCooldown] = {}
        self._restore_cooldowns()
        self._client = httpx.AsyncClient(
            timeout=timeout_seconds,
            headers={"User-Agent": user_agent, "Accept": "application/json"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    def engine_health(self) -> dict[str, str]:
        """Engines currently on cooldown and why, with seconds remaining."""
        now = time.monotonic()
        snapshot = {}
        for engine, cooldown in list(self._cooldowns.items()):
            remaining = cooldown.expires - now
            if remaining <= 0:
                self._cooldowns.pop(engine, None)
                continue
            snapshot[engine] = f"{cooldown.reason} ({int(remaining)}s cooldown left)"
        return snapshot

    def _cool(self, engine: str, reason: str, seconds: float | None = None) -> None:
        duration = seconds if seconds is not None else _classify_failure(reason)
        expiry = time.monotonic() + duration
        existing = self._cooldowns.get(engine)
        # Never shorten an in-flight cooldown: a second failure is evidence, not a reset.
        if existing and existing.expires >= expiry:
            return
        self._cooldowns[engine] = _EngineCooldown(reason=reason, expires=expiry)
        if self.store is None:
            return
        try:
            # Persisted as wall clock: a monotonic expiry is meaningless in the next process.
            self.store.record_engine_cooldown(engine, reason, time.time() + duration)
        except Exception as exc:  # pragma: no cover - storage must not break search
            self.last_warnings.append(f"engine_cooldown_not_persisted:{exc}")

    def _restore_cooldowns(self) -> None:
        """Reload penalties from a previous process so a restart does not re-probe dead engines.

        Without this, every restart looks healthy for exactly as long as it takes to get blocked
        again, which is precisely the blind spot that made the original failure so hard to see.
        """
        if self.store is None:
            return
        try:
            active = self.store.active_engine_cooldowns()
        except Exception as exc:  # pragma: no cover - storage must not break search
            self.last_warnings.append(f"engine_cooldown_restore_failed:{exc}")
            return
        for engine, (reason, remaining) in active.items():
            self._cooldowns[engine] = _EngineCooldown(
                reason=f"{reason} (restored)", expires=time.monotonic() + remaining
            )

    def _available_engines(self, *, exclude: set[str] | None = None) -> list[str]:
        """Known-good engines minus anything cooling down or already over-represented."""
        if not self.healthy_engines:
            return []
        skip = set(self.engine_health()) | (exclude or set())
        return [engine for engine in self.healthy_engines if engine not in skip]

    async def search(
        self,
        query: str,
        *,
        page: int = 1,
        language: str | None = None,
        time_range: str | None = None,
        categories: str | None = None,
        limit: int = 10,
        engines: str | None = None,
    ) -> list[SearchResult]:
        self.last_warnings = []
        self.last_engine_health = {}
        cache_key = _cache_key(query, page, language, time_range, categories, limit, engines)
        if self.store:
            cached = self.store.get_search(cache_key, self.cache_ttl_seconds)
            if cached is not None:
                return cached

        params: dict[str, str | int] = {"q": query, "format": "json", "pageno": page}
        if language:
            params["language"] = language
        normalized_range = _normalize_time_range(time_range)
        if normalized_range:
            params["time_range"] = normalized_range
        if categories:
            params["categories"] = categories
        if engines:
            params["engines"] = engines

        results: list[SearchResult] = []
        seen: set[str] = set()
        engine_failures: list[str] = []
        pinned = bool(engines)

        for attempt in range(self.max_retries + 1):
            last = attempt == self.max_retries
            try:
                payload = await self._request(params)
            except SearXNGChallengeError:
                raise
            except SearXNGRateLimitedError as exc:
                # A declared Retry-After is an instruction, not a suggestion. Sleeping less than it
                # re-triggers the same block, and waiting longer than the run can afford is worse
                # than returning what the earlier attempts already produced.
                if last:
                    raise
                wait = exc.retry_after
                if wait is None:
                    await self._backoff(attempt)
                    continue
                if wait > self.max_retry_wait_seconds:
                    self.last_warnings.append(
                        f"searxng_rate_limited: retry_after {int(wait)}s exceeds the "
                        f"{int(self.max_retry_wait_seconds)}s retry budget; keeping earlier results"
                    )
                    break
                await asyncio.sleep(wait)
                continue
            except SearXNGError:
                if last:
                    raise
                await self._backoff(attempt)
                continue

            for failure in payload["failures"]:
                engine_failures.append(f"{failure[0]}: {failure[1]}")
                self._cool(failure[0], failure[1])

            engine_failures = list(dict.fromkeys(engine_failures))
            if engine_failures:
                warning = "search_engines_unresponsive:" + ", ".join(engine_failures[:12])
                # Append rather than reassign: a later attempt that succeeds cleanly must not erase
                # the collapse diagnosis raised by the attempt that needed widening.
                if warning not in self.last_warnings:
                    self.last_warnings.append(warning)

            remaining = limit - len(results)
            if remaining > 0:
                results.extend(
                    _parse_results(
                        payload["results"],
                        remaining,
                        seen,
                        start_rank=len(results) + 1,
                    )
                )

            if results:
                if pinned or last:
                    break
                collapse = _collapsed_engine(results, self.diversity_min_results)
                available = (
                    self._available_engines(exclude={collapse}) if collapse else []
                )
                if not available:
                    if collapse:
                        self.last_warnings.append(
                            f"search_engine_diversity_collapsed:all {len(results)} attributed "
                            f"results came from '{collapse}'; "
                            "no non-cooled engines available to widen the query"
                        )
                    break
                # Diversity is the point. If the collapsed engine already filled the quota, the
                # widened request would run and then be discarded, so free the lower half of the
                # results and let the healthy engines fill those slots instead.
                collapsed_count = len(results)
                del results[max(1, limit // 2) :]
                params["engines"] = ",".join(available)
                params.pop("categories", None)
                pinned = True
                self.last_warnings.append(
                    f"search_engine_diversity_collapsed:all {collapsed_count} attributed results "
                    f"came from '{collapse}'; re-queried pinned to {','.join(available)}"
                )
                continue

            # Engines that answered with nothing are an authoritative empty result. Only retry when
            # the emptiness is explained by engines that failed, otherwise this loop hammers a
            # genuinely thin query.
            if not engine_failures:
                break
            if last:
                raise SearXNGError(
                    "SearXNG returned no results because upstream engines were unresponsive after "
                    f"{attempt + 1} attempt(s): {', '.join(engine_failures)}"
                )
            await self._backoff(attempt)

        self.last_engine_health = self.engine_health()

        # Empty result sets are often transient when upstream engines are rate-limited or
        # challenged. Do not poison the cache with a temporary aggregate failure.
        if self.store and results:
            self.store.put_search(cache_key, results)
        return results

    async def _request(self, params: dict[str, str | int]) -> dict[str, Any]:
        """One HTTP round trip, classified. Raises for anything a retry cannot fix."""
        import httpx

        try:
            response = await self._client.get(f"{self.base_url}/search", params=params)
        except httpx.HTTPError as exc:
            raise SearXNGError(f"SearXNG request failed: {exc}") from exc

        status = response.status_code
        if status in (429, 503):
            retry_after = _retry_after_seconds(response)
            self._cool("searxng", f"HTTP {status}", retry_after)
            raise SearXNGRateLimitedError(
                f"SearXNG is rate limiting this client (HTTP {status})"
                + (f", retry after {int(retry_after)}s" if retry_after else ""),
                retry_after=retry_after,
            )
        if status == 403:
            raise SearXNGChallengeError(
                "SearXNG refused the request with HTTP 403. For a JSON query this normally means "
                "'json' is missing from search.formats in settings.yml, or the limiter classified "
                "this client as a bot."
            )
        if status >= 400:
            raise SearXNGError(f"SearXNG returned HTTP {status}")

        content_type = response.headers.get("content-type", "")
        body = response.text
        if "json" not in content_type.casefold():
            marker = next((m for m in _CHALLENGE_MARKERS if m in body.casefold()), "")
            raise SearXNGChallengeError(
                "SearXNG answered with text/html instead of JSON"
                + (f" (anti-bot page: '{marker}')" if marker else " (not a JSON payload)")
                + "; a challenge page needs a browser, so retrying will not help"
            )
        try:
            decoded = json.loads(body)
        except json.JSONDecodeError as exc:
            raise SearXNGChallengeError(f"SearXNG returned malformed JSON: {exc}") from exc
        if not isinstance(decoded, dict):
            raise SearXNGError("SearXNG returned a non-object JSON response")
        if not isinstance(decoded.get("results"), list):
            raise SearXNGError("SearXNG returned no JSON results array")
        return {
            "results": decoded["results"],
            "failures": _unresponsive_engines(decoded),
        }

    async def _backoff(self, attempt: int) -> None:
        """Exponential backoff with jitter.

        A flat sub-second delay is the worst possible cadence against a rate limit: it looks like
        a scripted retry loop. Full jitter spreads attempts so consecutive queries do not stack.
        """
        if not self.retry_base_seconds:
            return
        ceiling = self.retry_base_seconds * (2**attempt)
        await asyncio.sleep(random.uniform(ceiling / 2, ceiling))


def _parse_results(
    raw_results: list[Any],
    limit: int,
    seen: set[str],
    *,
    start_rank: int = 1,
) -> list[SearchResult]:
    results: list[SearchResult] = []
    for rank, item in enumerate(raw_results, start=start_rank):
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
    return results


def _collapsed_engine(results: list[SearchResult], min_results: int) -> str | None:
    """Return the sole engine behind a result set, or None when diversity is acceptable.

    Only conclusive when the payload actually carries per-result engine attribution: results with
    no engine metadata are unknown, not collapsed. Engine failures are judged separately, because
    a single-engine answer set is still a collapse even when nothing reported an error.
    """
    if len(results) < min_results:
        return None
    counts: dict[str, int] = {}
    attributed = 0
    for result in results:
        engines = result.engines
        if not engines:
            continue
        attributed += 1
        for engine in engines:
            counts[engine] = counts.get(engine, 0) + 1
    if attributed < min_results or len(counts) > 1:
        return None
    return next(iter(counts))


def _retry_after_seconds(response: Any) -> float | None:
    value = response.headers.get("retry-after", "").strip()
    if not value:
        return None
    try:
        return max(1.0, float(value))
    except ValueError:
        return None


def _parse_engine_list(value: str | None) -> list[str]:
    if not value:
        return []
    seen: set[str] = set()
    engines: list[str] = []
    for part in value.split(","):
        name = part.strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            engines.append(name)
    return engines


def _unresponsive_engines(payload: dict[str, Any]) -> list[tuple[str, str]]:
    raw = payload.get("unresponsive_engines")
    if not isinstance(raw, list):
        return []
    failures: list[tuple[str, str]] = []
    for item in raw[:12]:
        if isinstance(item, (list, tuple)):
            parts = [str(value).strip() for value in item[:2] if str(value).strip()]
            engine = parts[0] if parts else "unknown"
            reason = parts[1] if len(parts) > 1 else "unresponsive"
            pair = (engine, reason)
        elif isinstance(item, dict):
            pair = (
                str(item.get("engine") or item.get("name") or "unknown").strip(),
                str(item.get("error") or item.get("message") or "unresponsive").strip(),
            )
        else:
            pair = (str(item).strip()[:64], "unresponsive")
        if pair not in failures:
            failures.append((pair[0][:64], pair[1][:240]))
    return failures


def _cache_key(
    query: str,
    page: int,
    language: str | None,
    time_range: str | None,
    categories: str | None,
    limit: int,
    engines: str | None = None,
) -> str:
    value = json.dumps(
        [
            query.strip(),
            page,
            language or "",
            time_range or "",
            categories or "",
            limit,
            engines or "",
        ],
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
