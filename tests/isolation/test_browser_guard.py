from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from web_research.isolation import transport
from web_research.readers import crawl4ai
from web_research.readers.actions import ActionRun
from web_research.readers.crawl4ai import Crawl4AIReader


class _Served(httpx.AsyncBaseTransport):
    """An isolated browser has no direct route: anything not aborted goes through the broker."""

    async def handle_async_request(self, request):
        return httpx.Response(200, headers={"content-type": "font/woff2"}, content=b"wOF2")


async def setup_guard(monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_ISOLATED_WORKER", "1")
    context = SimpleNamespace(
        clear_cookies=AsyncMock(), route=AsyncMock(), route_web_socket=AsyncMock()
    )
    browser = SimpleNamespace(new_context=AsyncMock(return_value=context))

    async def create_context():
        return await browser.new_context(ignore_https_errors=True)

    manager = SimpleNamespace(browser=browser, create_browser_context=create_context)
    hooks = {}
    strategy = SimpleNamespace(
        browser_manager=manager, set_hook=lambda name, fn: hooks.update({name: fn})
    )
    reader = Crawl4AIReader()
    reader._install_network_guard(SimpleNamespace(crawler_strategy=strategy))
    await manager.create_browser_context()
    browser.new_context.assert_awaited_once_with(
        ignore_https_errors=False,
        service_workers="block",
        accept_downloads=False,
    )
    await hooks["on_page_context_created"](object(), context)
    return context.route.call_args.args[1], context


@pytest.mark.parametrize(
    "method,body,resource,url,frame",
    [
        # A page may post to its own origin, but not to somebody else's.
        ("POST", b"q=x", "fetch", "https://api.example.com/graphql", "https://example.com/app"),
        ("POST", b"q" * 70_000, "fetch", "https://example.com/api", "https://example.com/app"),
        ("POST", None, "fetch", "https://example.com/api", "https://example.com/app"),
        ("PUT", b"q=x", "fetch", "https://example.com/api", "https://example.com/app"),
        ("GET", b"secret", "fetch", "https://example.com", "https://example.com"),
        ("GET", None, "media", "https://example.com/clip.mp4", "https://example.com"),
        ("GET", None, "font", "https://example.com/f.woff2", "https://example.com"),
    ],
)
async def test_browser_blocks_requests_the_broker_cannot_carry(
    monkeypatch, method, body, resource, url, frame
):
    guard, _ = await setup_guard(monkeypatch)
    request = SimpleNamespace(
        method=method,
        post_data_buffer=body,
        resource_type=resource,
        url=url,
        headers={},
        frame=SimpleNamespace(url=frame),
    )
    route = SimpleNamespace(request=request, abort=AsyncMock(), fulfill=AsyncMock())
    await guard(route)
    route.abort.assert_awaited_once()
    route.fulfill.assert_not_awaited()


async def test_browser_posts_to_its_own_origin_through_the_broker(monkeypatch):
    seen = []

    class FakeBroker(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            seen.append((request.method, str(request.url), request.read()))
            return httpx.Response(200, content=b'{"results":[]}')

    monkeypatch.setattr(transport, "BrokerTransport", FakeBroker)
    guard, _ = await setup_guard(monkeypatch)
    route = SimpleNamespace(
        request=SimpleNamespace(
            method="POST",
            post_data_buffer=b'{"q":"x"}',
            resource_type="fetch",
            url="https://example.com/api/search",
            headers={"content-type": "application/json"},
            frame=SimpleNamespace(url="https://example.com/search?q=x"),
        ),
        abort=AsyncMock(),
        fulfill=AsyncMock(),
        continue_=AsyncMock(),
    )
    await guard(route)
    assert seen == [("POST", "https://example.com/api/search", b'{"q":"x"}')]
    route.fulfill.assert_awaited_once()
    route.abort.assert_not_awaited()


async def test_browser_reads_are_fulfilled_by_broker_and_websockets_closed(monkeypatch):
    requests = []

    class FakeBroker(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            requests.append(str(request.url))
            return httpx.Response(200, content=b"rendered source")

    monkeypatch.setattr(transport, "BrokerTransport", FakeBroker)
    guard, context = await setup_guard(monkeypatch)
    route = SimpleNamespace(
        request=SimpleNamespace(
            method="GET",
            post_data_buffer=None,
            resource_type="document",
            url="https://example.com",
            headers={},
            frame=SimpleNamespace(url="https://example.com"),
        ),
        abort=AsyncMock(),
        fulfill=AsyncMock(),
        continue_=AsyncMock(),
    )
    await guard(route)
    assert requests == ["https://example.com"]
    route.fulfill.assert_awaited_once_with(
        status=200, headers={"content-length": "15"}, body=b"rendered source"
    )
    route.continue_.assert_not_awaited()
    socket = SimpleNamespace(close=AsyncMock())
    await context.route_web_socket.call_args.args[1](socket)
    socket.close.assert_awaited_once()


async def test_broker_failure_has_no_direct_browser_fallback(monkeypatch):
    class FailedBroker(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError("broker stopped")

    monkeypatch.setattr(transport, "BrokerTransport", FailedBroker)
    guard, _ = await setup_guard(monkeypatch)
    route = SimpleNamespace(
        request=SimpleNamespace(
            method="GET",
            post_data_buffer=None,
            resource_type="document",
            url="https://example.com",
            headers={},
            frame=SimpleNamespace(url="https://example.com"),
        ),
        abort=AsyncMock(),
        fulfill=AsyncMock(),
        continue_=AsyncMock(),
    )
    await guard(route)
    route.abort.assert_awaited_once()
    route.continue_.assert_not_awaited()


def _resource_request(resource: str):
    return SimpleNamespace(
        method="GET",
        post_data_buffer=None,
        resource_type=resource,
        url="https://example.com/widget.woff2"
        if resource == "font"
        else "https://example.com/c.mp4",
        headers={},
        frame=SimpleNamespace(url="https://example.com"),
    )


@pytest.mark.parametrize("resource,expected", [("font", "abort"), ("media", "abort")])
async def test_a_read_that_only_wants_text_gives_up_its_fonts_and_media(
    monkeypatch, resource, expected
):
    monkeypatch.setattr(transport, "BrokerTransport", _Served)
    guard, _ = await setup_guard(monkeypatch)
    route = SimpleNamespace(
        request=_resource_request(resource),
        abort=AsyncMock(),
        fulfill=AsyncMock(),
        continue_=AsyncMock(),
    )

    await guard(route)

    getattr(route, expected).assert_awaited_once()


async def test_a_read_that_will_click_gives_the_page_its_fonts(monkeypatch):
    """The reCAPTCHA checkbox is drawn from a webfont; a broken glyph is a broken target."""
    monkeypatch.setattr(transport, "BrokerTransport", _Served)
    guard, _ = await setup_guard(monkeypatch)
    run = ActionRun(plan=[], interactive=True)
    token = crawl4ai._ACTION_RUN.set(run)
    try:
        route = SimpleNamespace(
            request=_resource_request("font"),
            abort=AsyncMock(),
            fulfill=AsyncMock(),
            continue_=AsyncMock(),
        )
        await guard(route)
    finally:
        crawl4ai._ACTION_RUN.reset(token)

    route.abort.assert_not_awaited()
    route.fulfill.assert_awaited_once()
