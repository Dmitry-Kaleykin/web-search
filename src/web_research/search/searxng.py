"""SearXNG-backed search provider.

Beyond the plain JSON contract this layer tracks upstream engine health. SearXNG reports failing
engines in ``unresponsive_engines`` but still returns HTTP 200 with whatever survived, so a
single reachable index can answer every query while the rest of the fleet is blocked. Retrying the
identical request cannot fix that: a CAPTCHA wall and a rate-limit suspension both ignore fast
retries, and nested retries deepen the block.

This provider therefore (a) never retries an anti-bot challenge, (b) honours ``Retry-After``,
(c) puts failing engines on a cooldown. Each call makes at most one network request.
The caller sees the diagnostics and decides whether to change engines or query.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

from ..dates import normalize_published_at
from ..models import SearchResult
from ..safety.urls import canonicalize_url
from ..storage import SQLiteStore
from .health import EngineHealth


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


class SearXNGSearchProvider:
    def __init__(
        self,
        base_url: str,
        *,
        store: SQLiteStore | None = None,
        cache_ttl_seconds: int = 900,
        timeout_seconds: float = 20.0,
        user_agent: str = "LocalResearchBot/0.1",
        healthy_engines: str = "",
        use_catalog: bool = False,
    ) -> None:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - dependency error is user-facing
            raise RuntimeError("httpx is required; install the project dependencies") from exc
        self.base_url = base_url.rstrip("/")
        self.store = store
        self.cache_ttl_seconds = cache_ttl_seconds
        self.healthy_engines = _parse_engine_list(healthy_engines)
        self.last_cache_hit = False
        self.last_retrieved_at: str | None = None
        self.last_warnings: list[str] = []
        self.last_engine_health: dict[str, str] = {}
        self.health = EngineHealth(store)
        self.use_catalog = use_catalog
        self.timeout_seconds = timeout_seconds
        self.catalog = store.get_backend_metadata(self.base_url) if store else None
        self._client = httpx.AsyncClient(
            timeout=timeout_seconds,
            headers={"User-Agent": user_agent, "Accept": "application/json"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    def engine_report(self) -> dict[str, dict]:
        names = list(dict.fromkeys([*(self.healthy_engines or self.health.records()), "searxng"]))
        report = self.health.snapshot(names)
        inventory = (self.catalog or {}).get("engines")
        for name in self.healthy_engines:
            report[name]["configuration_verified"] = inventory is not None
            if inventory is not None and (name not in inventory or not inventory[name]["enabled"]):
                report[name].update(
                    status="disabled" if name in inventory else "missing",
                    reason="Engine is not enabled in the SearXNG configuration",
                )
        return report

    def engine_health(self) -> dict[str, str]:
        now = time.time()
        return {
            name: f"{entry['reason']} ({max(0, int((entry['retry_at'] or now) - now))}s; "
            f"{entry['status']})"
            for name, entry in self.engine_report().items()
            if entry["status"] in {"cooling_down", "recovering", "disabled", "missing"}
        }

    def _cool(self, engine: str, reason: str, seconds: float | None = None) -> None:
        self.health.failed(engine, reason, seconds)

    async def refresh_catalog(self, *, force: bool = False) -> None:
        """Read only local backend metadata; failed inspection never certifies configuration."""
        if not force and self.catalog and self.catalog.get("refresh_after", 0) > time.time():
            return
        try:
            response = await self._client.get(f"{self.base_url}/config", timeout=2)
            response.raise_for_status()
            payload = response.json()
            entries = payload["engines"]
            if not isinstance(entries, list) or not entries:
                raise ValueError("missing engine inventory")
            self.catalog = {
                "engines": {
                    item["name"]: {
                        "enabled": item.get("enabled") is True,
                        "paging": item.get("paging") is True,
                        "time_range_support": item.get("time_range_support") is True,
                    }
                    for item in entries
                    if isinstance(item, dict) and isinstance(item.get("name"), str)
                },
                "version": payload.get("version"),
                "refresh_after": time.time() + 900,
            }
        except Exception as exc:
            # Catalog availability must not disable an otherwise working JSON search service.
            self.catalog = {"error": type(exc).__name__, "refresh_after": time.time() + 60}
            self.last_warnings.append(
                "engine_configuration_unverified: backend /config unavailable"
            )
        if self.store:
            self.store.put_backend_metadata(self.base_url, self.catalog)

    def _filter_catalog(self, params: dict) -> None:
        inventory = (self.catalog or {}).get("engines")
        if inventory is None or not params.get("engines"):
            return
        requested = _parse_engine_list(params["engines"])
        available = []
        for name in requested:
            entry = inventory.get(name)
            reason = (
                "missing"
                if not entry
                else "disabled"
                if not entry["enabled"]
                else "paging_unsupported"
                if params["pageno"] > 1 and not entry["paging"]
                else "time_filter_unsupported"
                if params.get("time_range") and not entry["time_range_support"]
                else None
            )
            if reason:
                self.last_warnings.append(f"search_engine_skipped:{name}:{reason}")
            else:
                available.append(name)
        if not available:
            raise SearXNGError("No configured engine can execute these search parameters")
        params["engines"] = ",".join(available)

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
        refresh: bool = False,
    ) -> list[SearchResult]:
        self.last_cache_hit = False
        self.last_retrieved_at = None
        self.last_warnings = []
        self.last_engine_health = {}
        engines = engines or ",".join(self.healthy_engines) or None
        cache_key = _cache_key(query, page, language, time_range, categories, limit, engines)
        if self.store and not refresh:
            cached = self.store.get_search_entry(cache_key, self.cache_ttl_seconds)
            if cached is not None:
                self.last_cache_hit = True
                self.last_retrieved_at = cached.retrieved_at
                self.last_warnings.extend(cached.warnings)
                self.last_engine_health = self.engine_health()
                return cached.results

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

        cooling = {name.casefold(): reason for name, reason in self.engine_health().items()}
        if "searxng" in cooling:
            raise SearXNGRateLimitedError(f"SearXNG endpoint is cooling down: {cooling['searxng']}")
        if engines:
            requested = _parse_engine_list(engines)
            available = [name for name in requested if name.casefold() not in cooling]
            if not available:
                raise SearXNGChallengeError(
                    "All requested search engines are cooling down or excluded by configuration"
                )
            skipped = [name for name in requested if name.casefold() in cooling]
            if skipped:
                self.last_warnings.append(
                    "search_engines_skipped_at_retrieval:" + ", ".join(skipped)
                )
            params["engines"] = ",".join(available)
        # A failed engine must prove recovery. One engine recovery per discovery request;
        # SQLite leases prevent another process from probing that same engine concurrently.
        leases = {}
        statuses = self.health.snapshot(["searxng", *_parse_engine_list(params.get("engines"))])
        attempted = _parse_engine_list(params.get("engines"))
        recovered_engine_selected = False
        for name in ["searxng", *attempted]:
            if statuses[name]["status"] != "recovery_due":
                continue
            if name != "searxng" and recovered_engine_selected:
                attempted.remove(name)
                self.last_warnings.append(f"engine_recovery_deferred:{name}")
                continue
            token = self.health.claim(name, self.timeout_seconds + 5)
            if token:
                leases[name] = token
                recovered_engine_selected |= name != "searxng"
            elif name == "searxng":
                raise SearXNGError("SearXNG recovery already in progress")
            else:
                attempted.remove(name)
        if params.get("engines"):
            if not attempted:
                for name, token in leases.items():
                    self.health.release(name, token)
                raise SearXNGError("Requested engines are waiting for a recovery attempt")
            params["engines"] = ",".join(attempted)
        started = time.monotonic()
        started_at = self.health.clock()
        try:
            payload = await self._request(params)
            self.last_retrieved_at = datetime.now(UTC).isoformat()
            self.health.succeeded(
                "searxng",
                len(payload["results"]),
                (time.monotonic() - started) * 1000,
                started_at=started_at,
            )
            failures = list(dict.fromkeys(tuple(failure) for failure in payload["failures"]))
            for engine, reason in failures:
                self._cool(engine, reason)
            if failures:
                self.last_warnings.append(
                    "search_engines_unresponsive:"
                    + ", ".join(f"{engine}: {reason}" for engine, reason in failures[:12])
                )
            failed = {name for name, _ in failures}
            counts = {}
            for item in payload["results"]:
                if not isinstance(item, dict):
                    continue
                names = item.get("engines") or item.get("engine") or []
                if not isinstance(names, (list, str)):
                    continue
                for name in [names] if isinstance(names, str) else names:
                    if isinstance(name, str) and name in attempted:
                        counts[name] = counts.get(name, 0) + 1
            timings = payload.get("timings", {})
            for name in set(counts) | (set(timings) & set(attempted)):
                if name not in failed:
                    self.health.succeeded(
                        name, counts.get(name, 0), timings.get(name), started_at=started_at
                    )
            results = _parse_results(payload["results"], limit, set())
            if not results and failures:
                raise SearXNGError(
                    "SearXNG returned no results because upstream engines were unresponsive: "
                    + ", ".join(f"{engine}: {reason}" for engine, reason in failures)
                )
        finally:
            for name, token in leases.items():
                self.health.release(name, token, inconclusive=True)

        self.last_engine_health = self.engine_health()

        # Empty result sets are often transient when upstream engines are rate-limited or
        # challenged. Do not poison the cache with a temporary aggregate failure.
        if self.store and results:
            self.store.put_search(
                cache_key,
                results,
                retrieved_at=self.last_retrieved_at,
                warnings=self.last_warnings,
            )
        return results

    async def _request(self, params: dict[str, str | int]) -> dict[str, Any]:
        """One HTTP round trip, classified. Raises for anything a retry cannot fix."""
        import httpx

        if self.use_catalog:
            await self.refresh_catalog()
            self._filter_catalog(params)
        try:
            response = await self._client.get(f"{self.base_url}/search", params=params)
        except httpx.HTTPError as exc:
            self._cool("searxng", "connection failure")
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
            self._cool("searxng", "HTTP 403 access denied")
            raise SearXNGChallengeError(
                "SearXNG refused the request with HTTP 403. For a JSON query this normally means "
                "'json' is missing from search.formats in settings.yml, or the limiter classified "
                "this client as a bot."
            )
        if status >= 400:
            self._cool("searxng", f"HTTP {status}")
            raise SearXNGError(f"SearXNG returned HTTP {status}")

        content_type = response.headers.get("content-type", "")
        body = response.text
        if "json" not in content_type.casefold():
            self._cool("searxng", "blocked: non-JSON response")
            marker = next((m for m in _CHALLENGE_MARKERS if m in body.casefold()), "")
            raise SearXNGChallengeError(
                "SearXNG answered with text/html instead of JSON"
                + (f" (anti-bot page: '{marker}')" if marker else " (not a JSON payload)")
                + "; a challenge page needs a browser, so retrying will not help"
            )
        try:
            decoded = json.loads(body)
        except json.JSONDecodeError as exc:
            self._cool("searxng", "malformed JSON response")
            raise SearXNGChallengeError(f"SearXNG returned malformed JSON: {exc}") from exc
        if not isinstance(decoded, dict):
            self._cool("searxng", "invalid JSON response")
            raise SearXNGError("SearXNG returned a non-object JSON response")
        if not isinstance(decoded.get("results"), list):
            self._cool("searxng", "missing JSON results array")
            raise SearXNGError("SearXNG returned no JSON results array")
        return {
            "results": decoded["results"],
            "failures": _unresponsive_engines(decoded),
            "timings": _engine_timings(response.headers.get("server-timing", "")),
        }


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
                url=str(item["url"]),
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


def _engine_timings(value: str) -> dict[str, float]:
    # SearXNG emits total_<index>_<engine>;dur=<milliseconds>, including engines
    # that executed successfully but returned zero hits. Unsupported filters have no timing.
    timings = {}
    for part in value.split(","):
        match = re.fullmatch(r"\s*total_\d+_(.+?);dur=([0-9.]+)\s*", part)
        if match:
            try:
                duration = float(match[2])
                if math.isfinite(duration) and duration >= 0:
                    timings[match[1]] = duration
            except ValueError:
                pass
    return timings


def _retry_after_seconds(response: Any) -> float | None:
    value = response.headers.get("retry-after", "").strip()
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            seconds = date.timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(1.0, seconds) if math.isfinite(seconds) else None


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
            normalized = normalize_published_at(value)
            if normalized:
                return normalized
    return None
