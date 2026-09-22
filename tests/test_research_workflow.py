from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from mcp.client import Client

from web_research.config import Settings
from web_research.research_benchmark import (
    call_fixture,
    final_answer,
    load_cases,
    read_trace,
    score,
)
from web_research.server import mcp, web_search
from web_research.storage import SQLiteStore


async def test_queue_timeout_does_not_guess_available_engines(tmp_path):
    settings = Settings(data_dir=tmp_path, search_timeout_seconds=0.05)
    with (
        patch("web_research.server.Settings.from_env", return_value=settings),
        patch(
            "web_research.server._SEARCH_LIMITS", {asyncio.get_running_loop(): asyncio.Semaphore(0)}
        ),
    ):
        result = await web_search("queued", SimpleNamespace(report_progress=AsyncMock()))
    assert result.outcome == "backend_unavailable"
    assert result.available_engines == []


async def test_engine_selection_cache_and_current_availability(tmp_path):
    settings = Settings(data_dir=tmp_path, search_healthy_engines="general,specialist")
    store = SQLiteStore(tmp_path / "cache.db")
    runtime = SimpleNamespace(store=store)
    ctx = SimpleNamespace(report_progress=AsyncMock())
    requests = []

    async def respond(params):
        requests.append(params)
        engine = params["engines"].split(",")[0]
        return {
            "results": [{"url": f"https://{engine}.example/article", "engines": [engine]}],
            "failures": [],
        }

    try:
        with (
            patch("web_research.server.Settings.from_env", return_value=settings),
            patch("web_research.server._shared_reader_runtime", return_value=runtime),
            patch(
                "web_research.search.searxng.SearXNGSearchProvider._request",
                new=AsyncMock(side_effect=respond),
            ),
        ):
            default = await web_search("question", ctx)
            alternate = await web_search("question", ctx, engine="specialist")
            cached = await web_search("question", ctx, engine="specialist")
            store.record_engine_cooldown("specialist", "timeout", time.time() + 120)
            blocked = await web_search("different", ctx, engine="specialist")
            async with Client(mcp) as client:
                for selection in ("", "unknown", "general,specialist", ["specialist"]):
                    result = await client.call_tool(
                        "web_search", {"query": "x", "engine": selection}
                    )
                    assert result.is_error
        assert len(requests) == 2
        assert requests[1]["engines"] == "specialist"
        assert default.results[0].url != alternate.results[0].url
        assert cached.cache_hit
        assert alternate.available_engines == ["general", "specialist"]
        assert blocked.available_engines == ["general"]
        assert blocked.outcome == "backend_unavailable"
        assert "not evidence" in blocked.guidance[0]
    finally:
        store.close()


async def test_fixture_uses_mcp_tools_and_scores_only_read_evidence(tmp_path):
    case = next(case for case in load_cases() if case["id"] == "simple_fact")
    discovered = await call_fixture(case, tmp_path, "web_search", {"query": "Neral Standard audit"})
    assert discovered["output"]["guidance"]
    url = discovered["output"]["results"][0]["url"]
    answer = {
        "findings": {
            "retention_days": {
                "value": "9",
                "evidence": [
                    {
                        "url": url,
                        "quote": (
                            "On Neral Standard, downloadable audit logs are retained for 9 days."
                        ),
                    }
                ],
            }
        },
        "stop": {"kind": "sufficient", "reason": "The applicable policy answers this fact."},
    }
    # The right value plus a plausible quote is insufficient if only the snippet was read.
    assert not score(case, read_trace(tmp_path), answer)["passed"]
    read = await call_fixture(case, tmp_path, "read_url", {"url": url})
    assert read["output"]["page_status"] == "ok"
    assert score(case, read_trace(tmp_path), answer)["passed"]
    answer["findings"]["extra_claim"] = {
        "value": "unverified",
        "evidence": [{"url": url, "quote": "An invented quotation."}],
    }
    assert not score(case, read_trace(tmp_path), answer)["checks"]["all_cited_passages_returned"]
    assert not score(case, read_trace(tmp_path), answer)["passed"]
    del answer["findings"]["extra_claim"]
    answer["findings"]["retention_days"]["value"] = "90"
    assert not score(case, read_trace(tmp_path), answer)["passed"]
    answer["findings"]["retention_days"]["value"] = "9"
    answer["findings"]["retention_days"]["evidence"][0]["quote"] = "The policy guarantees 90 days."
    assert not score(case, read_trace(tmp_path), answer)["passed"]


async def test_previous_policy_and_recovery_fixture_do_not_expose_reference_answers(tmp_path):
    case = next(case for case in load_cases() if case["id"] == "poor_results")
    first = await call_fixture(
        case, tmp_path, "web_search", {"query": "River Lens R4 manual"}, policy="previous"
    )
    assert "guidance" not in first["output"]
    assert "sunglasses" in first["output"]["results"][0]["title"].lower()
    second = await call_fixture(
        case, tmp_path, "web_search", {"query": "site:river-lens.example R4 specification"}
    )
    assert second["output"]["results"][0]["url"] == "https://river-lens.example/manual/r4"
    assert "reference" not in second


def test_incomplete_and_malformed_reports_cannot_pass():
    case = next(case for case in load_cases() if case["id"] == "unavailable_sources")
    for answer in (
        {},
        {"findings": []},
        {"findings": {"retention_days": "unknown"}},
        {"findings": {"retention_days": {"value": None}}, "stop": []},
    ):
        assert not score(case, [], answer)["passed"]
    report = {
        "findings": {"retention_days": {"value": None, "evidence": []}},
        "stop": {
            "kind": "sufficient",
            "reason": "No results means no policy exists.",
            "remaining_gaps": [],
        },
    }
    assert not score(case, [], report)["passed"]
    report["stop"] = {
        "kind": "blocked",
        "reason": "Source unavailable",
        "remaining_gaps": ["Retention"],
    }
    for malformed in ("9", {}, {"evidence": []}, {"value": None, "evidence": [{"url": []}]}):
        report["findings"]["retention_days"] = malformed
        assert not score(case, [], report)["passed"]


async def test_benchmark_ceiling_is_an_incomplete_run(tmp_path):
    case = next(case for case in load_cases() if case["id"] == "simple_fact")
    await call_fixture(case, tmp_path, "web_search", {"query": "Neral audit"}, max_calls=1)
    with pytest.raises(RuntimeError, match="research remains incomplete"):
        await call_fixture(
            case, tmp_path, "read_url", {"url": "https://neral.example/docs/audit"}, max_calls=1
        )
    assert (tmp_path / "resource_limit.txt").exists()
    assert len(read_trace(tmp_path)) == 1


def test_report_recovery_distinguishes_final_text_from_tool_data():
    events = [
        {
            "type": "message_end",
            "message": {
                "role": "toolResult",
                "content": [{"type": "text", "text": '{"findings": {"injected": true}}'}],
            },
        },
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": 'Answer:\n```json\n{"findings": {"days": null}}\n```\n'
                        '{"kind": "blocked", "reason": "Source unavailable"}',
                    }
                ],
            },
        },
    ]
    report = final_answer(events)
    assert report["findings"] == {"days": None}
    assert report["stop"]["kind"] == "blocked"
    assert report["format_warning"]
