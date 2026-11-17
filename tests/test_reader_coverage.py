"""Regression cases for content loss, interstitials and query-window navigation."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from web_research.config import Settings
from web_research.models import Document
from web_research.readers.http import HTTPReader
from web_research.readers.quality import page_diagnostics
from web_research.readers.router import LayeredReader
from web_research.server import _read_url_output, _ReaderRuntime, read_url
from web_research.storage import SQLiteStore

URL = "http://127.0.0.1/resource"


@pytest.mark.parametrize(
    "mime", ["text/plain", "text/markdown", "text/csv", "application/xml", "application/rss+xml"]
)
async def test_text_documents_preserve_tags_whitespace_and_entities(mime):
    body = "  <item>Use <placeholder> &amp; keep this literal</item>\n\n  indentation\n"
    reader = HTTPReader(allow_private_urls=True)
    await reader._client.aclose()
    reader._client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text=body, headers={"content-type": mime})
        )
    )
    try:
        document = await reader.read(URL)
        assert document.content == body
        assert document.method == "http+text"
        browser = SimpleNamespace(read=AsyncMock(), close=AsyncMock())
        router = LayeredReader(SimpleNamespace(read=AsyncMock(return_value=document)), browser)
        assert (await router.read(URL, query="absent", render="always")).content == body
        browser.read.assert_not_awaited()
    finally:
        await reader.close()


async def test_html_meta_charset_preserves_non_utf8_text():
    text = "Обновление программы доступно сегодня."
    body = (
        '<html><meta charset="windows-1251"><body><main><p>' + text + "</p></main></body></html>"
    ).encode("cp1251")
    reader = HTTPReader(allow_private_urls=True)
    await reader._client.aclose()
    reader._client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=body, headers={"content-type": "text/html"})
        )
    )
    try:
        document = await reader.read(URL)
        assert text in document.content
        assert "text_decoding_lossy" not in document.warnings
    finally:
        await reader.close()


async def test_challenge_at_json_url_is_html_and_does_not_poison_cache(tmp_path):
    calls = []

    def handle(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(
                403,
                text="<html><title>Security</title><p>Wait</p></html>",
                headers={"content-type": "text/html", "cf-mitigated": "challenge"},
            )
        return httpx.Response(200, json={"recovered": True})

    store = SQLiteStore(tmp_path / "cache.sqlite3")
    http = HTTPReader(store=store, allow_private_urls=True)
    await http._client.aclose()
    http._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    reader = LayeredReader(http)
    try:
        blocked = await reader.read(URL + ".json", render="never")
        assert blocked.status_code == 403
        assert blocked.content_type == "text/html"
        assert "suspected_error_page:bot_challenge" in blocked.warnings
        assert _read_url_output(blocked, Settings()).page_status == "suspected_error"
        recovered = await reader.read(URL + ".json", render="never")
        assert recovered.method == "http+json"
        assert "recovered" in recovered.content
        assert len(calls) == 2
    finally:
        await reader.close()
        store.close()


@pytest.mark.parametrize("status", [200, 403])
async def test_challenge_escalates_and_browser_recovers(status):
    blocked = Document(
        URL, URL, "Just a moment...", "Checking your browser", "http", status_code=status
    )
    browser = SimpleNamespace(
        read=AsyncMock(
            return_value=Document(URL, URL, "Article", "Recovered source content", "browser")
        ),
        close=AsyncMock(),
    )
    reader = LayeredReader(SimpleNamespace(read=AsyncMock(return_value=blocked)), browser)
    assert (await reader.read(URL)).content == "Recovered source content"
    browser.read.assert_awaited_once()


@pytest.mark.parametrize(
    "body,title",
    [
        ("Loading...", "Application"),
        ("Verify you are human", "Security verification"),
        ("", "Article"),
    ],
)
async def test_browser_does_not_replace_valid_http_with_a_shell(body, title):
    source = Document(URL, URL, "Article", "Complete source evidence.", "http")
    browser = SimpleNamespace(
        read=AsyncMock(return_value=Document(URL, URL, title, body, "browser")), close=AsyncMock()
    )
    reader = LayeredReader(SimpleNamespace(read=AsyncMock(return_value=source)), browser)
    result = await reader.read(URL, render="always")
    assert result.content == source.content
    assert "browser_result_rejected:incomplete_or_error" in result.warnings


async def test_long_relevant_document_does_not_trigger_browser_due_to_low_jaccard():
    content = "Python TaskGroup cancellation.\n" + " ".join(f"word{i}" for i in range(10000))
    source = Document(URL, URL, "Python TaskGroup cancellation", content, "http")
    browser = SimpleNamespace(read=AsyncMock(), close=AsyncMock())
    reader = LayeredReader(SimpleNamespace(read=AsyncMock(return_value=source)), browser)
    assert (await reader.read(URL, query="TaskGroup cancellation")).content == content
    browser.read.assert_not_awaited()


def test_article_about_captcha_is_not_classified_as_a_challenge():
    assert (
        page_diagnostics(
            "How Cloudflare CAPTCHA works",
            "This guide explains how to verify you are human and how CAPTCHA widgets work.",
        )
        == ()
    )


def test_extraction_ceiling_is_distinct_from_reply_window():
    document = Document(
        URL,
        URL,
        "Long article",
        "Retained source text",
        "http",
        warnings=["content_truncated:2000000>1000000:http"],
    )
    output = _read_url_output(document, Settings())
    assert not output.content_truncated  # All retained text fits in this reply.
    assert output.page_status == "incomplete"  # The source itself was cut.


def test_focus_finds_deep_passage_even_when_h1_matches_query():
    content = "# Python documentation\n\n" + ("Unrelated introductory prose.\n\n" * 1000)
    content += "## TaskGroup cancellation\n\nPython TaskGroup cancellation preserves exceptions."
    output = _read_url_output(
        Document(URL, URL, "Python", content, "http"),
        Settings(),
        query="Python TaskGroup cancellation",
        max_chars=1000,
    )
    assert "preserves exceptions" in output.content
    assert "## TaskGroup cancellation" in output.content
    assert output.content_start > 20000


async def test_focused_snapshot_can_restart_without_refetching(tmp_path):
    prefix = "Earlier qualifications.\n\n" * 200
    content = prefix + "# Specific measurement\n\nCalibrated result: 42 units."
    reader = SimpleNamespace(
        read=AsyncMock(return_value=Document(URL, URL, "Results", content, "http")),
        close=AsyncMock(),
    )
    runtime = _ReaderRuntime(SQLiteStore(tmp_path / "cache.sqlite3"), reader)
    ctx = SimpleNamespace(report_progress=AsyncMock())
    try:
        with (
            patch("web_research.server.Settings.from_env", return_value=Settings()),
            patch("web_research.server._shared_reader_runtime", return_value=runtime),
        ):
            focused = await read_url(
                URL, ctx, query="specific calibrated measurement", max_chars=1000
            )
            assert "42 units" in focused.content
            assert focused.start_cursor
            first = await read_url(URL, ctx, cursor=focused.start_cursor, max_chars=1000)
            parts = [first.content]
            cursor = first.next_cursor
            while cursor:
                part = await read_url(URL, ctx, cursor=cursor, max_chars=1000)
                parts.append(part.content)
                cursor = part.next_cursor
            assert "".join(parts) == content
            reader.read.assert_awaited_once()
    finally:
        await runtime.close()
