"""The pinned SearXNG process shares its stderr with whoever supervises the MCP server."""

from __future__ import annotations

import contextlib
import io

from web_research.isolation import searx_worker


def test_the_search_access_log_reports_the_route_without_the_query():
    handler = searx_worker.RouteOnlyLog.__new__(searx_worker.RouteOnlyLog)
    handler.command = "GET"
    handler.path = "/search?q=private%20research%20terms&format=json&engines=mojeek"
    handler.client_address = ("127.0.0.1", 4321)

    captured = io.StringIO()
    with contextlib.redirect_stderr(captured):
        handler.log_request(200, 46_302)

    line = captured.getvalue()
    assert '"GET /search" 200 46302' in line, line
    assert "private" not in line and "mojeek" not in line, line


def test_the_broker_result_wait_is_shorter_than_the_brokers_own_budget():
    from web_research.isolation.transport import BROKER_TIMEOUT_SECONDS
    from web_research.isolation.worker import BROKER_RESULT_TIMEOUT

    assert BROKER_RESULT_TIMEOUT < BROKER_TIMEOUT_SECONDS
