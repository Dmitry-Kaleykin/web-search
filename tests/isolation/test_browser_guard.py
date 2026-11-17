from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from web_research.isolation import transport
from web_research.readers.crawl4ai import Crawl4AIReader


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
    "method,body,resource",
    [
        ("POST", None, "fetch"),
        ("GET", b"secret", "fetch"),
        ("GET", None, "media"),
    ],
)
async def test_browser_blocks_write_requests_before_broker(monkeypatch, method, body, resource):
    guard, _ = await setup_guard(monkeypatch)
    request = SimpleNamespace(method=method, post_data_buffer=body, resource_type=resource)
    route = SimpleNamespace(request=request, abort=AsyncMock(), fulfill=AsyncMock())
    await guard(route)
    route.abort.assert_awaited_once()
    route.fulfill.assert_not_awaited()


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
        ),
        abort=AsyncMock(),
        fulfill=AsyncMock(),
        continue_=AsyncMock(),
    )
    await guard(route)
    route.abort.assert_awaited_once()
    route.continue_.assert_not_awaited()
