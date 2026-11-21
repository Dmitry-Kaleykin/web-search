from __future__ import annotations

import asyncio
import base64
import json
import tempfile
import time

import httpx
import pytest

from web_research.isolation import transport
from web_research.isolation.transport import MAX_REQUEST_BODY_BYTES, BrokerFailure


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE", "CONNECT", "OPTIONS"])
async def test_write_methods_never_reach_broker(method, monkeypatch):
    monkeypatch.setattr(transport, "SOCKET_PATH", "/nonexistent-broker")
    async with httpx.AsyncClient(transport=transport.BrokerTransport()) as client:
        with pytest.raises(httpx.UnsupportedProtocol, match="upload/write"):
            await client.request(method, "https://example.com", content=b"private")


async def test_get_body_never_reaches_broker(monkeypatch):
    monkeypatch.setattr(transport, "SOCKET_PATH", "/nonexistent-broker")
    async with httpx.AsyncClient(transport=transport.BrokerTransport()) as client:
        with pytest.raises(httpx.UnsupportedProtocol, match="GET and HEAD"):
            await client.request("GET", "https://example.com", content=b"private")


@pytest.mark.parametrize(
    "headers,body,expect",
    [
        ({"content-type": "application/json"}, b'{"q":"x"}', None),
        ({"content-type": "application/x-www-form-urlencoded; charset=utf-8"}, b"q=x", None),
        ({"content-type": "text/plain"}, b"q=x", None),
        ({"content-type": "application/octet-stream"}, b"q=x", "form, JSON or text"),
        ({}, b"q=x", "form, JSON or text"),
    ],
)
def test_post_bodies_are_validated_before_the_boundary(headers, body, expect):
    request = httpx.Request("POST", "https://example.com/search", headers=headers, content=body)
    if expect is None:
        payload = transport.request_payload(request, body)
        assert payload["method"] == "POST" and payload["body"] == body.decode()
        return
    with pytest.raises(httpx.RequestError, match=expect):
        transport.request_payload(request, body)


def test_oversized_post_and_credentials_are_refused_locally():
    request = httpx.Request(
        "POST",
        "https://example.com/search",
        headers={"content-type": "application/json"},
        content=b"x" * (MAX_REQUEST_BODY_BYTES + 1),
    )
    with pytest.raises(httpx.RequestError, match="POST limit"):
        transport.request_payload(request, b"x" * (MAX_REQUEST_BODY_BYTES + 1))
    credentialed = httpx.Request(
        "GET", "https://example.com", headers={"authorization": "Bearer secret"}
    )
    with pytest.raises(httpx.RequestError, match="credentials"):
        transport.request_payload(credentialed)


@pytest.fixture
def short_socket_path():
    # macOS Unix-domain socket paths are limited to 104 bytes.
    with tempfile.TemporaryDirectory(prefix="ws-", dir="/tmp") as directory:
        yield directory + "/broker.sock"


@pytest.mark.parametrize("sync", [False, True])
async def test_binary_and_duplicate_headers_survive_local_rpc(short_socket_path, monkeypatch, sync):
    path = short_socket_path
    monkeypatch.setattr(transport, "SOCKET_PATH", path)
    captured = []

    async def serve(reader, writer):
        captured.append(json.loads(await reader.readline()))
        writer.write(
            json.dumps(
                {
                    "status": 200,
                    "headers": [
                        ["content-type", "application/pdf"],
                        ["x-test", "a"],
                        ["x-test", "b"],
                    ],
                    "body": base64.b64encode(b"%PDF-\x00\xff").decode(),
                }
            ).encode()
            + b"\n"
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(serve, path)
    try:
        if sync:

            def request():
                with httpx.Client(transport=transport.SyncBrokerTransport()) as client:
                    return client.get("https://example.com/report.pdf")

            response = await asyncio.to_thread(request)
        else:
            async with httpx.AsyncClient(transport=transport.BrokerTransport()) as client:
                # SearXNG can pass an unread, empty request stream even for GET.
                class EmptyStream(httpx.AsyncByteStream):
                    async def __aiter__(self):
                        yield b""

                response = await client.request(
                    "GET", "https://example.com/report.pdf", content=EmptyStream()
                )
        assert response.content == b"%PDF-\x00\xff"
        assert response.headers.get_list("x-test") == ["a", "b"]
        assert captured[0]["method"] == "GET"
        assert "body" not in captured[0]
    finally:
        server.close()
        await server.wait_closed()


def test_policy_errors_are_not_successful_pages():
    request = httpx.Request("GET", "https://example.com")
    with pytest.raises(BrokerFailure, match="Non-public") as raised:
        transport.decode_response({"error": "Non-public destination is blocked"}, request)
    # The kind is how a caller avoids blaming a website for a local decision.
    assert raised.value.kind == "transport"
    assert isinstance(raised.value, httpx.TransportError)
    with pytest.raises(BrokerFailure) as too_large:
        transport.decode_response(
            {"error": "Response exceeds the native byte limit", "error_kind": "too_large"}, request
        )
    assert too_large.value.kind == "too_large"


def test_reported_header_adjustments_reach_the_response():
    request = httpx.Request("GET", "https://example.com")
    response = transport.decode_response(
        {
            "status": 200,
            "headers": [["content-type", "text/html"]],
            "body": base64.b64encode(b"<html></html>").decode(),
            "filtered": ["authorization", "-set-cookie"],
            "forwarded": ["cookie", "referer"],
        },
        request,
    )
    assert response.extensions["broker_filtered"] == ["authorization", "-set-cookie"]
    assert response.extensions["broker_forwarded"] == ["cookie", "referer"]


async def test_the_caller_timeout_ends_the_wait_and_releases_the_request(
    short_socket_path, monkeypatch
):
    """A caller that gives up must not occupy the broker for its whole transfer budget."""
    path = short_socket_path
    monkeypatch.setattr(transport, "SOCKET_PATH", path)
    answered = asyncio.Event()

    async def serve(reader, writer):
        await reader.readline()
        await asyncio.sleep(0.6)  # a broker still busy with a request nobody wants
        answered.set()
        writer.close()
        await writer.wait_closed()

    started = time.monotonic()
    server = await asyncio.start_unix_server(serve, path)
    try:
        async with httpx.AsyncClient(
            transport=transport.BrokerTransport(), timeout=httpx.Timeout(0.15)
        ) as client:
            # httpx only *hands* its timeout to a custom transport, it does not enforce one, so
            # the transport honouring the deadline is the behaviour under test.
            with pytest.raises(httpx.TimeoutException):
                await client.get("https://example.com/slow")
        assert time.monotonic() - started < 0.4
        assert not answered.is_set()
    finally:
        server.close()
        await server.wait_closed()


def test_the_broker_timeout_covers_its_own_transfer_budget():
    # The native side can queue briefly, then connect, then transfer. A shorter local wait would
    # report a timeout for work the broker is still doing, and nobody would ever learn why.
    assert transport.BROKER_TIMEOUT_SECONDS > 55
    request = httpx.Request("GET", "https://example.com", extensions={"timeout": {"read": 3.0}})
    assert transport._wait_seconds(request) == 3.0
    assert transport._wait_seconds(httpx.Request("GET", "https://example.com")) == (
        transport.BROKER_TIMEOUT_SECONDS
    )


async def test_a_real_socket_round_trip_agrees_on_framing(short_socket_path, monkeypatch):
    """Transport and worker were written separately; only real bytes prove they agree.

    Mocked streams hide the one class of bug that stops every isolated request: both ends reading
    or writing the line differently, which looks like a website failure from the outside.
    """
    from web_research.isolation import worker

    path = short_socket_path
    pending: dict[str, asyncio.Future] = {}

    async def emit(value: dict) -> None:
        pending[value["id"]].set_result(
            {
                "status": 200,
                "headers": [["content-type", "text/plain"]],
                "body": base64.b64encode(b"rendered").decode(),
                "forwarded": ["cookie"],
                "filtered": ["authorization"],
            }
        )

    server = await asyncio.start_unix_server(
        lambda reader, writer: worker.broker_request(
            reader, writer, emit=emit, pending=pending, slots=asyncio.Semaphore(4)
        ),
        path,
        limit=worker.MAX_REQUEST_LINE + 1,
    )
    monkeypatch.setattr(transport, "SOCKET_PATH", path)
    try:
        async with httpx.AsyncClient(transport=transport.BrokerTransport(), timeout=5) as client:
            response = await client.get("https://example.com/", headers={"cookie": "session=a"})
    finally:
        server.close()
        await server.wait_closed()

    assert response.status_code == 200
    assert response.text == "rendered"
    assert response.extensions["broker_forwarded"] == ["cookie"]
    assert response.extensions["broker_filtered"] == ["authorization"]
    assert pending == {}


async def test_a_page_larger_than_the_old_reply_buffer_survives_the_round_trip(
    short_socket_path, monkeypatch
):
    """The reply buffer used to be 160 KB, which is smaller than one ordinary article.

    A base64 body is a third larger than the bytes it carries, so anything past about 118 KB was
    truncated mid-line and reported as a failed fetch. That is a large fraction of real pages, and
    the router hid it by escalating to Chromium, which reads as a slow website rather than a bug.
    """
    from web_research.isolation import worker

    path = short_socket_path
    pending: dict[str, asyncio.Future] = {}
    article = b"<html><body>" + b"x" * 200_000 + b"</body></html>"

    async def emit(value: dict) -> None:
        pending[value["id"]].set_result(
            {
                "status": 200,
                "headers": [["content-type", "text/html"]],
                "body": base64.b64encode(article).decode(),
            }
        )

    server = await asyncio.start_unix_server(
        lambda reader, writer: worker.broker_request(
            reader, writer, emit=emit, pending=pending, slots=asyncio.Semaphore(4)
        ),
        path,
        limit=worker.MAX_REQUEST_LINE + 1,
    )
    monkeypatch.setattr(transport, "SOCKET_PATH", path)
    try:
        async with httpx.AsyncClient(transport=transport.BrokerTransport(), timeout=5) as client:
            response = await client.get("https://example.com/article")
    finally:
        server.close()
        await server.wait_closed()

    assert response.content == article
    assert pending == {}
