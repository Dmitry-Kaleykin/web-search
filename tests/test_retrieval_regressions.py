from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from web_research.cli import _public_read_report
from web_research.config import Settings
from web_research.models import Document, SearchResult
from web_research.readers.http import HTTPReader
from web_research.storage import SQLiteStore

HTML = (Path(__file__).parent / "fixtures/relative_links.html").read_text()


@pytest.mark.parametrize("base", [None, "../manual/"])
async def test_redirected_page_preserves_relative_links_in_markdown_and_link_list(base):
    final_url = "https://example.com/docs/lab/procedure.html"
    html = HTML.replace("<head>", f'<head><base href="{base}">') if base else HTML

    def handler(request):
        if request.url.path == "/old":
            return httpx.Response(302, headers={"location": final_url})
        return httpx.Response(200, text=html, headers={"content-type": "text/html"})

    reader = HTTPReader()
    await reader._client.aclose()
    reader._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with patch("web_research.readers.http.validate_public_url", new_callable=AsyncMock) as dns:
            dns.return_value = SimpleNamespace(addresses=("93.184.216.34",))
            document = await reader.read("https://example.com/old")
    finally:
        await reader.close()

    effective_base = "https://example.com/docs/manual/" if base else final_url
    directory = "https://example.com/docs/manual/" if base else "https://example.com/docs/lab/"
    targets = [
        directory + "instruments.html#calibration",
        effective_base + "#results",
        "https://example.com/docs/index.html",
        effective_base + "?view=full#appendix",
        "https://example.com/about.html",
        "https://other.example/reference",
    ]
    assert document.final_url == final_url
    assert document.method == "http+trafilatura"
    for target in targets:
        assert f"]({target})" in document.content
        assert target in document.links


async def test_http_reader_does_not_reuse_extractions_with_old_broken_links(tmp_path):
    url = "https://example.com/docs/lab/procedure.html"
    store = SQLiteStore(tmp_path / "cache.db")
    store.put_document(
        url, Document(url=url, final_url=url, title="Old", content="old", method="http")
    )
    reader = HTTPReader(store=store)
    await reader._client.aclose()
    reader._client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text=HTML, headers={"content-type": "text/html"})
        )
    )
    try:
        with patch("web_research.readers.http.validate_public_url", new_callable=AsyncMock) as dns:
            dns.return_value = SimpleNamespace(addresses=("93.184.216.34",))
            document = await reader.read(url)
        assert "instrument calibration" in document.content
        assert "cache_hit" not in document.warnings
    finally:
        await reader.close()
        store.close()


def test_search_provenance_survives_store_reopen_and_legacy_entries_remain_honest(tmp_path):
    path = tmp_path / "cache.db"
    results = [SearchResult(url="https://example.com", title="Example")]
    retrieved_at = "2026-09-18T01:00:00+00:00"
    warnings = ["search_engines_unresponsive:engine-a: timeout"]
    store = SQLiteStore(path)
    store.put_search("new", results, retrieved_at=retrieved_at, warnings=warnings)
    with store._connection:
        store._connection.execute(
            "INSERT INTO search_cache VALUES (?, ?, ?)",
            ("old", time.time(), json.dumps([asdict(item) for item in results])),
        )
    store.close()
    reopened = SQLiteStore(path)
    try:
        cached = reopened.get_search_entry("new", 900)
        assert cached.retrieved_at == retrieved_at
        assert cached.warnings == warnings
        assert reopened.get_search("new", 900) == results
        legacy = reopened.get_search_entry("old", 900)
        assert legacy.results == results
        assert legacy.retrieved_at is None
        assert "diagnostics unavailable" in legacy.warnings[0]
    finally:
        reopened.close()


@pytest.mark.parametrize("failure", ["proxy", "soft_error", "wrong_content", None])
async def test_doctor_requires_fresh_public_content_and_closes_reader(failure, capsys):
    document = Document(
        url="https://docs.python.org/3/library/asyncio-task.html",
        final_url="https://docs.python.org/3/library/asyncio-task.html",
        title="404 Not Found" if failure == "soft_error" else "Coroutines and tasks",
        content="Sign in to continue" if failure == "wrong_content" else "TaskGroup documentation",
        method="http+trafilatura",
    )
    reader = SimpleNamespace(read=AsyncMock(return_value=document))
    runtime = SimpleNamespace(reader=reader, close=AsyncMock())
    if failure == "proxy":
        reader.read.side_effect = ValueError("Synthetic proxy DNS address is blocked")
    with patch("web_research.server._create_reader_runtime", return_value=runtime):
        result = await _public_read_report(Settings())
    assert result is (failure is None)
    reader.read.assert_awaited_once_with(document.url, max_age_seconds=0)
    runtime.close.assert_awaited_once()
    captured = capsys.readouterr()
    assert ("FAIL public URL read" in captured.err) is (failure is not None)
