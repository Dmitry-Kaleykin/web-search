from __future__ import annotations

import asyncio
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from email.utils import format_datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from web_research.config import Settings
from web_research.search.health import EngineHealth
from web_research.search.searxng import SearXNGSearchProvider, _retry_after_seconds
from web_research.server import close_reader_runtimes, web_search
from web_research.storage import SQLiteStore


@pytest.fixture
def store(tmp_path):
    database = SQLiteStore(tmp_path / "cache.db")
    yield database
    database.close()


@asynccontextmanager
async def provider(store, handler, **kwargs):
    search = SearXNGSearchProvider(
        "http://search.test", store=store, healthy_engines="general,other", **kwargs
    )
    await search._client.aclose()
    search._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        yield search
    finally:
        await search.close()


def test_migrates_old_database_without_losing_block_or_journal(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE engine_health(engine TEXT PRIMARY KEY, reason TEXT NOT NULL,
                expires_at REAL NOT NULL, updated_at REAL NOT NULL);
            INSERT INTO engine_health VALUES ('general','access denied',9999999999,10);
            CREATE TABLE events (payload TEXT);
            INSERT INTO events VALUES ('historical');
        """)
    database = SQLiteStore(path)
    try:
        health = EngineHealth(database).snapshot(["general"])["general"]
        assert health["status"] == "cooling_down"
        assert health["consecutive_failures"] == 1
        assert (
            database._connection.execute("SELECT payload FROM events").fetchone()[0] == "historical"
        )
    finally:
        database.close()


def test_failure_backoff_survives_expiry_restart_and_maintenance(store):
    now = [time.time()]
    health = EngineHealth(store, clock=lambda: now[0])
    health.failed("general", "timeout")
    first = health.snapshot(["general"])["general"]
    now[0] = first["retry_at"] + 1
    store.maintenance()
    second = EngineHealth(store, clock=lambda: now[0])
    assert second.snapshot(["general"])["general"]["status"] == "recovery_due"
    second.failed("general", "timeout")
    row = second.snapshot(["general"])["general"]
    assert row["consecutive_failures"] == 2
    assert row["retry_at"] - now[0] > first["retry_at"] - first["last_failure"]
    second.succeeded("general", 0, 42)
    row = second.snapshot(["general"])["general"]
    assert row["status"] == "working"
    assert row["consecutive_failures"] == 0
    assert row["failures"] == 2
    assert row["last_latency_ms"] == 42


def test_suspension_echo_does_not_count_as_another_failed_attempt(store):
    health = EngineHealth(store)
    health.failed("general", "access denied")
    health.failed("general", "Suspended: access denied")
    row = health.snapshot(["general"])["general"]
    assert row["consecutive_failures"] == 1
    assert row["failures"] == 1


def test_recovery_lease_is_exclusive_across_connections_and_expires(store):
    now = [time.time()]
    first = EngineHealth(store, clock=lambda: now[0])
    first.failed("general", "timeout")
    now[0] += 121
    peer_store = SQLiteStore(store.path)
    try:
        peer = EngineHealth(peer_store, clock=lambda: now[0])
        token = first.claim("general", 20)
        assert token
        assert peer.claim("general", 20) is None
        assert peer.snapshot(["general"])["general"]["status"] == "recovering"
        now[0] += 21
        assert peer.claim("general", 20)
        first.release("general", token)
        assert peer.snapshot(["general"])["general"]["status"] == "recovering"
    finally:
        peer_store.close()


async def test_empty_response_needs_execution_evidence_to_confirm_recovery(store):
    clock = [time.time()]
    use_timings = False

    def respond(request):
        return httpx.Response(
            200,
            json={"results": []},
            headers=({"server-timing": "total_0_general;dur=8.2"} if use_timings else {}),
        )

    async with provider(store, respond) as search:
        search.health.clock = lambda: clock[0]
        search.health.failed("general", "timeout")
        clock[0] += 121
        await search.search("nothing", engines="general")
        assert search.engine_report()["general"]["consecutive_failures"] == 1
        assert search.engine_report()["general"]["status"] == "cooling_down"
        clock[0] += 121
        use_timings = True
        assert await search.search("nothing", engines="general") == []
        assert search.engine_report()["general"]["status"] == "working"
        assert search.engine_report()["general"]["failures"] == 1


async def test_only_one_recovery_engine_is_probed_per_request(store):
    clock = [time.time()]
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            200, json={"results": [{"url": "https://example.com/a", "engines": ["general"]}]}
        )

    async with provider(store, respond) as search:
        search.health.clock = lambda: clock[0]
        for name in ["general", "other"]:
            search.health.failed(name, "timeout")
        clock[0] += 121
        await search.search("recover")
        assert calls[0].url.params["engines"] == "general"
        assert search.engine_report()["general"]["status"] == "working"
        assert search.engine_report()["other"]["status"] == "recovery_due"


async def test_cached_result_does_not_verify_recovery_or_reprobe(store):
    count = 0

    def respond(request):
        nonlocal count
        count += 1
        return httpx.Response(
            200, json={"results": [{"url": "https://example.com/a", "engines": ["general"]}]}
        )

    async with provider(store, respond) as search:
        await search.search("cached")
        search.health.failed("general", "access denied")
        before = search.engine_report()["general"]
        await search.search("cached")
        assert count == 1
        assert search.last_cache_hit
        assert search.engine_report()["general"] == before


async def test_catalog_filters_missing_disabled_and_unsupported_without_marking_failure(store):
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path == "/config":
            return httpx.Response(
                200,
                json={
                    "engines": [
                        {
                            "name": "general",
                            "enabled": True,
                            "paging": True,
                            "time_range_support": True,
                        },
                        {
                            "name": "other",
                            "enabled": False,
                            "paging": False,
                            "time_range_support": False,
                        },
                    ]
                },
            )
        return httpx.Response(
            200, json={"results": []}, headers={"server-timing": "total_0_general;dur=20"}
        )

    async with provider(store, respond, use_catalog=True) as search:
        await search.search("catalog", page=2)
        assert requests[-1].url.params["engines"] == "general"
        assert search.engine_report()["other"]["status"] == "disabled"
        assert search.engine_report()["other"]["failures"] == 0
        await search.search("another")
        assert sum(r.url.path == "/config" for r in requests) == 1
    # Persisted inventory is reusable by a new process, not just this provider instance.
    async with provider(store, respond, use_catalog=True) as search:
        await search.search("third")
    assert sum(r.url.path == "/config" for r in requests) == 1


async def test_catalog_failure_keeps_search_usable_and_health_unverified(store):
    def respond(request):
        if request.url.path == "/config":
            return httpx.Response(404)
        return httpx.Response(200, json={"results": []})

    async with provider(store, respond, use_catalog=True) as search:
        assert await search.search("q") == []
        assert search.engine_report()["general"]["configuration_verified"] is False
        assert search.engine_report()["general"]["status"] == "untested"


def test_retry_after_accepts_http_date_and_rejects_nonfinite():
    date = format_datetime(datetime.fromtimestamp(time.time() + 7200, UTC), usegmt=True)
    response = httpx.Response(429, headers={"retry-after": date})
    assert 7190 < _retry_after_seconds(response) <= 7200
    for text in ["nan", "inf", "invalid"]:
        assert _retry_after_seconds(httpx.Response(429, headers={"retry-after": text})) is None


async def test_cancelled_recovery_releases_lease_without_inventing_failure(store):
    clock = [time.time()]
    entered = asyncio.Event()

    async def respond(request):
        entered.set()
        await asyncio.Event().wait()

    async with provider(store, respond) as search:
        search.health.clock = lambda: clock[0]
        search.health.failed("general", "timeout")
        clock[0] += 121
        task = asyncio.create_task(search.search("q", engines="general"))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        row = search.engine_report()["general"]
        assert row["status"] != "recovering"
        assert row["failures"] == 1


async def test_identical_mcp_refreshes_share_request_and_one_cancel_does_not_stop_peer(tmp_path):
    entered, finish = asyncio.Event(), asyncio.Event()

    async def respond(params):
        entered.set()
        await finish.wait()
        return {
            "results": [{"url": "https://example.com/a", "engines": ["general"]}],
            "failures": [],
        }

    settings = Settings(data_dir=tmp_path, search_healthy_engines="general", enable_crawl4ai=False)
    ctx = SimpleNamespace(report_progress=AsyncMock())
    try:
        with (
            patch("web_research.server.Settings.from_env", return_value=settings),
            patch(
                "web_research.search.searxng.SearXNGSearchProvider._request",
                AsyncMock(side_effect=respond),
            ) as request,
        ):
            first = asyncio.create_task(web_search("same", ctx, refresh=True))
            await entered.wait()
            second = asyncio.create_task(web_search("same", ctx, refresh=True))
            await asyncio.sleep(0)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            finish.set()
            result = await second
            assert result.outcome == "success"
            assert result.engine_status["general"]["status"] == "working"
            request.assert_awaited_once()
    finally:
        await close_reader_runtimes()


def test_late_success_cannot_erase_newer_failure(store):
    now = [time.time()]
    health = EngineHealth(store, clock=lambda: now[0])
    started = now[0]
    now[0] += 1
    health.failed("general", "access denied")
    now[0] += 1
    health.succeeded("general", 10, started_at=started)
    assert health.snapshot(["general"])["general"]["status"] == "cooling_down"


async def test_unsupported_filter_does_not_dispatch_or_mark_engine_broken(store):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "engines": [
                    {
                        "name": "general",
                        "enabled": True,
                        "paging": False,
                        "time_range_support": False,
                    },
                    {"name": "other", "enabled": False},
                ]
            },
        )

    async with provider(store, respond, use_catalog=True) as search:
        with pytest.raises(Exception, match="No configured engine can execute"):
            await search.search("q", page=2)
        assert [request.url.path for request in requests] == ["/config"]
        assert search.engine_report()["general"]["failures"] == 0
        assert search.engine_report()["general"]["status"] == "untested"


async def test_last_cancel_stops_shared_search_and_next_call_can_run(tmp_path):
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def respond(params):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    settings = Settings(data_dir=tmp_path, search_healthy_engines="general", enable_crawl4ai=False)
    ctx = SimpleNamespace(report_progress=AsyncMock())
    try:
        with (
            patch("web_research.server.Settings.from_env", return_value=settings),
            patch(
                "web_research.search.searxng.SearXNGSearchProvider._request",
                AsyncMock(side_effect=respond),
            ),
        ):
            task = asyncio.create_task(web_search("q", ctx, refresh=True))
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert cancelled.is_set()
        with (
            patch("web_research.server.Settings.from_env", return_value=settings),
            patch(
                "web_research.search.searxng.SearXNGSearchProvider._request",
                AsyncMock(return_value={"results": [], "failures": []}),
            ),
        ):
            result = await web_search("q", ctx, refresh=True)
            assert result.outcome == "empty"
    finally:
        await close_reader_runtimes()
