from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from mcp.client import Client

from web_research.config import Settings
from web_research.models import Document
from web_research.readers.http import HTTPReader
from web_research.readers.router import LayeredReader
from web_research.safety.urls import canonicalize_url
from web_research.server import (
    _ReaderRuntime,
    _shared_reader_runtime,
    close_reader_runtimes,
    mcp,
    read_url,
)
from web_research.storage import SQLiteStore

URL = "https://example.com/article/?ref=v2&source=python"


async def test_http_fetch_preserves_exact_resource_and_refreshes(tmp_path):
    calls = []

    def handle(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={"version": len(calls)})

    store = SQLiteStore(tmp_path / "cache.sqlite3")
    reader = HTTPReader(store=store, allow_private_urls=True)
    await reader._client.aclose()
    reader._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    url = "http://127.0.0.1/docs/?ref=v2&source=python&a=2&a=1&sig=a%2Fb"
    try:
        first = await reader.read(url)
        cached = await reader.read(url)
        fresh = await reader.read(url, max_age_seconds=0)
        assert calls == [url, url]
        assert first.content == cached.content != fresh.content
        assert first.url == first.final_url == url
        assert canonicalize_url(url) == url
    finally:
        await reader.close()
        store.close()


async def test_focused_browser_reads_are_coalesced_and_cached(tmp_path):
    store = SQLiteStore(tmp_path / "cache.sqlite3")
    primary = SimpleNamespace(
        store=store,
        read=AsyncMock(return_value=Document(URL, URL, "Shell", "Loading...", "http")),
        close=AsyncMock(),
    )
    browser = SimpleNamespace(
        read=AsyncMock(return_value=Document(URL, URL, "Page", "Complete article", "browser")),
        close=AsyncMock(),
    )
    reader = LayeredReader(primary, browser)
    try:
        documents = await asyncio.gather(*[reader.read(URL, query="article") for _ in range(12)])
        await reader.read(URL, query="article")
        assert browser.read.await_count == 1
        assert len({id(item) for item in documents}) == 12
    finally:
        await reader.close()
        store.close()


async def test_cancelled_waiter_does_not_cancel_another_reader():
    started, release = asyncio.Event(), asyncio.Event()

    async def fetch(url):
        started.set()
        await release.wait()
        return Document(url, url, "Page", "Full article", "http")

    primary = SimpleNamespace(read=fetch, close=AsyncMock())
    reader = LayeredReader(primary)
    first = asyncio.create_task(reader.read(URL))
    await started.wait()
    second = asyncio.create_task(reader.read(URL))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    assert (await second).content == "Full article"
    await reader.close()


async def test_last_cancelled_waiter_stops_underlying_fetch():
    started, stopped = asyncio.Event(), asyncio.Event()

    async def fetch(url):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    reader = LayeredReader(SimpleNamespace(read=fetch, close=AsyncMock()))
    task = asyncio.create_task(reader.read(URL))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()
    assert not reader._pending
    await reader.close()


async def test_all_calls_share_runtime_and_shutdown_closes_it(tmp_path):
    settings = Settings(data_dir=tmp_path, enable_crawl4ai=False)
    first = _shared_reader_runtime(settings)
    assert first is _shared_reader_runtime(settings)
    await close_reader_runtimes()
    assert first.reader.primary._client.is_closed


async def test_pagination_survives_page_change_and_runtime_restart(tmp_path):
    settings = Settings(data_dir=tmp_path, enable_crawl4ai=False)
    store = SQLiteStore(tmp_path / "cache.sqlite3")
    original = "".join(f"Paragraph {i}: original evidence.\n" for i in range(500))
    reader = SimpleNamespace(
        read=AsyncMock(return_value=Document(URL, URL, "Article", original, "fixture")),
        close=AsyncMock(),
    )
    runtime = _ReaderRuntime(store, reader)
    ctx = SimpleNamespace(report_progress=AsyncMock())
    with (
        patch("web_research.server.Settings.from_env", return_value=settings),
        patch("web_research.server._shared_reader_runtime", return_value=runtime),
    ):
        first = await read_url(URL, ctx, max_chars=1000)
        store.close()
        runtime.store = SQLiteStore(tmp_path / "cache.sqlite3")
        reader.read.return_value = Document(URL, URL, "Changed", "completely changed", "fixture")
        content, cursor = first.content, first.next_cursor
        while cursor:
            part = await read_url(URL, ctx, cursor=cursor, max_chars=1000)
            content += part.content
            cursor = part.next_cursor
        assert content == original
        reader.read.assert_awaited_once()
        with pytest.raises(ValueError, match="different URL"):
            await read_url(URL + "&different=1", ctx, cursor=first.next_cursor)
        with pytest.raises(ValueError, match="Refresh"):
            await read_url(URL, ctx, cursor=first.next_cursor, refresh=True)
        runtime.store._connection.execute("UPDATE read_snapshots SET stored_at=0")
        with pytest.raises(ValueError, match="expired"):
            await read_url(URL, ctx, cursor=first.next_cursor)
        await runtime.close()


async def test_mcp_visual_output_contains_actual_image_blocks(tmp_path):
    import base64
    import io

    from PIL import Image

    stream = io.BytesIO()
    Image.new("RGB", (10, 10), "white").save(stream, format="PNG")
    store = SQLiteStore(tmp_path / "cache.sqlite3")
    document = Document(
        URL,
        URL,
        "Chart",
        "Chart shown in attached image.",
        "fixture",
        images=[
            {
                "page": 1,
                "mime_type": "image/png",
                "data": base64.b64encode(stream.getvalue()).decode(),
            }
        ],
    )
    runtime = _ReaderRuntime(
        store, SimpleNamespace(read=AsyncMock(return_value=document), close=AsyncMock())
    )
    with patch("web_research.server._shared_reader_runtime", return_value=runtime):
        async with Client(mcp) as client:
            result = await client.call_tool("read_url", {"url": URL, "visual": True})
            assert not result.is_error
            assert any(item.type == "image" for item in result.content)
            assert result.structured_content["visual_pages"] == [1]
            tools = await client.list_tools()
            assert [tool.name for tool in tools.tools] == ["read_url", "web_search"]
    await runtime.close()


def test_cache_variant_cannot_renew_old_evidence(tmp_path):
    store = SQLiteStore(tmp_path / "cache.sqlite3")
    try:
        document = Document(
            URL,
            URL,
            "Old page",
            "Historical content",
            "fixture",
            retrieved_at="2010-01-01T00:00:00Z",
        )
        store.put_document("new-focused-variant", document)
        assert store.get_document("new-focused-variant", 3600) is None
        token = store.put_snapshot(document)
        # Snapshots deliberately preserve old evidence; they are never a freshness claim.
        assert store.get_snapshot(token).retrieved_at == document.retrieved_at
    finally:
        store.close()


def test_snapshot_eviction_is_explicit_and_bounded(tmp_path):
    store = SQLiteStore(tmp_path / "cache.sqlite3")
    document = Document(URL, URL, "Page", "Article content", "fixture")
    try:
        oldest = store.put_snapshot(document)
        for _ in range(100):
            store.put_snapshot(document)
        assert store.stats()["read_snapshots"]["rows"] == 100
        with pytest.raises(ValueError, match="expired or was evicted"):
            store.get_snapshot(oldest)
    finally:
        store.close()


def test_browser_action_descriptors_exclude_forms_and_destructive_controls():
    from web_research.readers.actions import ReadAction, discover_actions

    html = (
        '<html><body><button role="tab">Examples</button><button>Load more</button>'
        '<button>Delete account</button><form><button role="tab">Submit</button>'
        "</form></body></html>"
    )
    actions = discover_actions(html)
    assert [action["kind"] for action in actions] == ["tab", "load_more"]
    assert all(action["selector"].startswith("html:nth-of-type(1)") for action in actions)
    with pytest.raises(ValueError):
        ReadAction(kind="click", selector="#delete")
    with pytest.raises(ValueError):
        ReadAction(kind="tab")


async def test_settings_changes_cannot_multiply_reader_limits(tmp_path):
    from dataclasses import replace

    settings = Settings(data_dir=tmp_path, enable_crawl4ai=True, browser_max_concurrent_renders=1)
    first = _shared_reader_runtime(settings)
    second = _shared_reader_runtime(replace(settings, user_agent="Changed agent"))
    try:
        assert first is not second
        assert first.reader._read_limit is second.reader._read_limit
        assert first.reader.browser._render_semaphore is second.reader.browser._render_semaphore
        assert first.reader.primary._document_limit is second.reader.primary._document_limit
    finally:
        await close_reader_runtimes()
