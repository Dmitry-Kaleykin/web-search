from __future__ import annotations

import asyncio
import base64
import json
import tempfile

import httpx
import pytest

from web_research.isolation import transport


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "CONNECT", "OPTIONS"])
async def test_write_methods_never_reach_broker(method, monkeypatch):
    monkeypatch.setattr(transport, "SOCKET_PATH", "/nonexistent-broker")
    async with httpx.AsyncClient(transport=transport.BrokerTransport()) as client:
        with pytest.raises(httpx.UnsupportedProtocol, match="upload/write"):
            await client.request(method, "https://example.com", content=b"private")


async def test_get_body_never_reaches_broker(monkeypatch):
    monkeypatch.setattr(transport, "SOCKET_PATH", "/nonexistent-broker")
    async with httpx.AsyncClient(transport=transport.BrokerTransport()) as client:
        with pytest.raises(httpx.UnsupportedProtocol, match="request bodies"):
            await client.request("GET", "https://example.com", content=b"private")


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
    with pytest.raises(httpx.ConnectError, match="Non-public"):
        transport.decode_response({"error": "Non-public destination is blocked"}, request)
