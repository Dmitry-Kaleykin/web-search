from __future__ import annotations

import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from mcp.client import Client

from web_research.config import Settings
from web_research.retrieval_baseline import _launcher_path, compare_reports, record_baseline
from web_research.server import mcp


async def test_benchmark_uses_isolated_stdio_and_retains_cache_provenance(tmp_path):
    settings = Settings(data_dir=tmp_path, enable_crawl4ai=False, search_healthy_engines="general")
    launcher = tmp_path / "web-search-isolated"
    launcher.touch()
    transport = object()
    fixture_client = Client(mcp)
    cases = [
        {
            "id": "discovery",
            "category": "documentation",
            "tool": "web_search",
            "arguments": {"query": "example", "refresh": True},
            "review": "Check relevance.",
        }
    ]
    with (
        patch("web_research.server.Settings.from_env", return_value=settings),
        patch(
            "web_research.search.searxng.SearXNGSearchProvider._request",
            AsyncMock(
                return_value={
                    "results": [{"url": "https://example.com", "engines": ["general"]}],
                    "failures": [],
                }
            ),
        ),
        patch("web_research.retrieval_baseline.stdio_client", return_value=transport) as stdio,
        patch("web_research.retrieval_baseline.Client", return_value=fixture_client) as connect,
        patch("httpx.AsyncClient.send", side_effect=AssertionError("unexpected network")),
    ):
        report = await record_baseline(cases, launcher=launcher, label="fixture")
    assert stdio.call_args.args[0].command == str(launcher.resolve())
    connect.assert_called_once_with(transport, mode="auto")
    assert report["environment"]["transport"] == "isolated_stdio"
    assert report["environment"]["tools_sha256"]
    assert report["label"] == "fixture"
    case = report["cases"][0]
    assert all(case["checks"].values())
    assert case["call"]["output_bytes"] > 0
    assert case["followups"][0]["output"]["cache_hit"]
    assert case["manual_review"]["relevant_results_at_5"] is None
    assert report["summary"]["failed_cases"] == 0
    assert report["summary"]["median_cache_elapsed_ms"] is not None


async def test_benchmark_records_blocks_and_missing_passages_separately(tmp_path):
    launcher = tmp_path / "web-search-isolated"
    launcher.touch()
    cases = [
        {
            "id": "blocked",
            "tool": "read_url",
            "arguments": {"url": "https://example.com"},
            "access_probe": True,
        },
        {
            "id": "missing",
            "tool": "read_url",
            "arguments": {"url": "https://example.com/docs"},
            "expected_text": "A qualification that extraction lost",
        },
    ]
    results = [
        {
            "is_error": False,
            "output": {"page_status": "suspected_error", "content": "Access denied"},
        },
        {"is_error": False, "output": {"page_status": "ok", "content": "An incomplete quotation"}},
    ]

    # Only the tool boundary is controlled here; exercise real report/check generation.
    async def call(_client, tool, arguments):
        return {
            "tool": tool,
            "arguments": arguments,
            "elapsed_ms": 5,
            "output_bytes": 80,
            "error": None,
            **results.pop(0),
        }

    fixture_client = Client(mcp)
    with (
        patch("web_research.retrieval_baseline.stdio_client", return_value=object()),
        patch("web_research.retrieval_baseline.Client", return_value=fixture_client),
        patch("web_research.retrieval_baseline._call", side_effect=call),
    ):
        report = await record_baseline(cases, launcher=launcher)
    assert report["summary"]["access_limited_cases"] == ["blocked"]
    assert report["cases"][0]["observation"]["page_status"] == "suspected_error"
    assert "page_ok" not in report["cases"][0]["checks"]
    assert not report["cases"][1]["checks"]["expected_text_present"]
    assert report["summary"]["failed_cases"] == 1


def test_comparison_does_not_compare_changed_requests():
    original = {
        "id": "a",
        "call": {
            "tool": "web_search",
            "arguments": {"query": "a"},
            "elapsed_ms": 20,
            "output": {"results": []},
        },
        "checks": {"completed": True},
    }
    previous = {"cases": [original]}
    current = {"cases": [copy.deepcopy(original)]}
    current["cases"][0]["call"]["elapsed_ms"] = 10
    compared = compare_reports(previous, current)[0]
    assert compared["comparable"]
    assert compared["elapsed_ms_delta"] == -10
    assert compared["output_bytes_delta"] == 0
    current["cases"][0]["call"]["arguments"]["query"] = "different"
    assert not compare_reports(previous, current)[0]["comparable"]


def test_missing_launcher_never_falls_back_to_host_network(tmp_path):
    with pytest.raises(ValueError, match="Isolated launcher not found"):
        _launcher_path(tmp_path / "missing")


def test_packaged_corpus_covers_languages_formats_and_access_limits():
    manifest = (
        Path(__file__).resolve().parents[1] / "src/web_research/benchmark_data/retrieval_cases.json"
    )
    cases = json.loads(manifest.read_text())
    assert len({case["id"] for case in cases}) == len(cases)
    categories = {case["category"] for case in cases}
    assert {
        "recent_news_en",
        "recent_news_ru",
        "obscure_fact",
        "regional_ru",
        "pdf",
        "tables",
        "cyrillic",
        "plain_text",
        "feed",
        "browser",
        "access_probe",
    } <= categories
