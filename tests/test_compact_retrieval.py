from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from mcp.client import Client

from web_research.config import Settings
from web_research.server import close_reader_runtimes, mcp, web_search


async def test_compact_and_detailed_mcp_outputs_preserve_evidence(tmp_path):
    settings = Settings(data_dir=tmp_path, enable_crawl4ai=False, search_healthy_engines="general")
    payload = {
        "results": [
            {"url": "https://example.com/a", "content": "Relevant passage", "engines": ["general"]}
        ],
        "failures": [],
    }
    try:
        with (
            patch("web_research.server.Settings.from_env", return_value=settings),
            patch(
                "web_research.search.searxng.SearXNGSearchProvider._request",
                AsyncMock(return_value=payload),
            ) as request,
            patch("httpx.AsyncClient.send", side_effect=AssertionError("unexpected network")),
        ):
            async with Client(mcp) as client:
                compact = await client.call_tool("web_search", {"query": "example"})
                detailed = await client.call_tool(
                    "web_search", {"query": "example", "diagnostics": True}
                )
        small, full = compact.structured_content, detailed.structured_content
        assert not compact.is_error and not detailed.is_error
        assert small["results"] == full["results"]
        assert small["retrieved_at"] == full["retrieved_at"]
        assert full["cache_hit"]
        for key in ("engine_status", "requested_engines", "elapsed_ms", "responded_at"):
            assert key not in small
            assert key in full
        assert "guidance" not in small
        assert json.loads(compact.content[0].text) == small
        assert len(json.dumps(small)) < len(json.dumps(full))
        request.assert_awaited_once()
    finally:
        await close_reader_runtimes()


async def test_cache_hit_bypasses_active_search_and_preserves_current_limitations(tmp_path):
    settings = Settings(data_dir=tmp_path, enable_crawl4ai=False, search_healthy_engines="general")
    ctx = SimpleNamespace(report_progress=AsyncMock())
    started, release = asyncio.Event(), asyncio.Event()

    async def respond(params):
        if params["q"] == "slow":
            started.set()
            await release.wait()
        return {
            "results": [{"url": "https://example.com/a", "engines": ["general"]}],
            "failures": [],
        }

    pending = None
    try:
        with (
            patch("web_research.server.Settings.from_env", return_value=settings),
            patch(
                "web_research.search.searxng.SearXNGSearchProvider._request",
                AsyncMock(side_effect=respond),
            ) as request,
            patch("httpx.AsyncClient.send", side_effect=AssertionError("unexpected network")),
        ):
            original = await web_search("cached", ctx)
            pending = asyncio.create_task(web_search("slow", ctx))
            await asyncio.wait_for(started.wait(), 1)
            from web_research.server import _shared_reader_runtime

            store = _shared_reader_runtime(settings).store
            store.record_engine_cooldown("general", "access denied", time.time() + 120)
            cached = await asyncio.wait_for(web_search("cached", ctx), 1)
            assert cached.cache_hit and not pending.done()
            assert cached.results == original.results
            assert cached.retrieved_at == original.retrieved_at
            assert cached.warnings == original.warnings
            assert "general" in cached.engine_health
            assert cached.available_engines == []
            release.set()
            await asyncio.wait_for(pending, 1)
            assert request.await_count == 2
    finally:
        if pending and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await close_reader_runtimes()


@pytest.mark.parametrize("expired,refresh", [(True, False), (False, True)])
async def test_expired_or_refreshed_search_still_waits_for_dispatch(tmp_path, expired, refresh):
    settings = Settings(
        data_dir=tmp_path,
        enable_crawl4ai=False,
        search_healthy_engines="general",
        search_timeout_seconds=0.05,
    )
    ctx = SimpleNamespace(report_progress=AsyncMock())
    try:
        with (
            patch("web_research.server.Settings.from_env", return_value=settings),
            patch(
                "web_research.search.searxng.SearXNGSearchProvider._request",
                AsyncMock(
                    return_value={
                        "results": [{"url": "https://example.com/a"}],
                        "failures": [],
                    }
                ),
            ) as request,
        ):
            await web_search("cached", ctx)
            if expired:
                from web_research.server import _shared_reader_runtime

                store = _shared_reader_runtime(settings).store
                with store._connection:
                    store._connection.execute("UPDATE search_cache SET stored_at = 0")
            with patch(
                "web_research.server._SEARCH_LIMITS",
                {asyncio.get_running_loop(): asyncio.Semaphore(0)},
            ):
                result = await web_search("cached", ctx, refresh=refresh)
            assert result.outcome == "backend_unavailable"
            assert not result.cache_hit
            assert result.available_engines == []
            request.assert_awaited_once()
    finally:
        await close_reader_runtimes()


async def test_diagnostic_choice_does_not_duplicate_shared_fresh_search(tmp_path):
    settings = Settings(data_dir=tmp_path, enable_crawl4ai=False, search_healthy_engines="general")
    ctx = SimpleNamespace(report_progress=AsyncMock())
    started, release = asyncio.Event(), asyncio.Event()

    async def respond(_params):
        started.set()
        await release.wait()
        return {"results": [], "failures": []}

    tasks = []
    try:
        with (
            patch("web_research.server.Settings.from_env", return_value=settings),
            patch(
                "web_research.search.searxng.SearXNGSearchProvider._request",
                AsyncMock(side_effect=respond),
            ) as request,
        ):
            tasks.append(asyncio.create_task(web_search("same", ctx, refresh=True)))
            await asyncio.wait_for(started.wait(), 1)
            tasks.append(
                asyncio.create_task(web_search("same", ctx, refresh=True, diagnostics=True))
            )
            # Let both callers register with the shared operation before completing it.
            await asyncio.sleep(0)
            release.set()
            compact, full = await asyncio.wait_for(asyncio.gather(*tasks), 1)
            assert compact.engine_status is None
            assert full.engine_status["general"]["status"] == "untested"
            request.assert_awaited_once()
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await close_reader_runtimes()
