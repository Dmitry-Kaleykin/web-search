"""HTTPX transport over the worker's local Unix socket, never an internet socket."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import socket

import httpx

SOCKET_PATH = "/tmp/web-search-broker.sock"
MAX_FRAME = 8_000_000


def isolated() -> bool:
    return os.environ.get("WEB_SEARCH_ISOLATED_WORKER") == "1"


def request_payload(request: httpx.Request) -> dict:
    # Reject locally as well as at the native boundary. Do not serialize request bodies.
    if request.method not in {"GET", "HEAD"}:
        raise httpx.UnsupportedProtocol("Web Search blocks upload/write methods", request=request)
    return {
        "url": str(request.url),
        "method": request.method,
        "headers": dict(request.headers),
    }


def decode_response(result: dict, request: httpx.Request) -> httpx.Response:
    if "error" in result:
        raise httpx.ConnectError("Network policy: " + result["error"], request=request)
    return httpx.Response(
        int(result["status"]),
        headers=result["headers"],
        content=base64.b64decode(result["body"], validate=True),
        request=request,
    )


class BrokerTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        payload = request_payload(request)
        async for chunk in request.stream:
            if chunk:
                raise httpx.UnsupportedProtocol("Web Search blocks request bodies", request=request)
        reader, writer = await asyncio.open_unix_connection(SOCKET_PATH, limit=MAX_FRAME)
        try:
            writer.write(json.dumps(payload).encode() + b"\n")
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout=65)
            if not line or len(line) > MAX_FRAME:
                raise httpx.ConnectError("Native network broker disconnected", request=request)
            return decode_response(json.loads(line), request)
        finally:
            writer.close()
            await writer.wait_closed()


class SyncBrokerTransport(httpx.BaseTransport):
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        payload = request_payload(request)
        for chunk in request.stream:
            if chunk:
                raise httpx.UnsupportedProtocol("Web Search blocks request bodies", request=request)
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(65)
            connection.connect(SOCKET_PATH)
            connection.sendall(json.dumps(payload).encode() + b"\n")
            with connection.makefile("rb") as stream:
                line = stream.readline(MAX_FRAME + 1)
            if not line or len(line) > MAX_FRAME:
                raise httpx.ConnectError("Native network broker disconnected", request=request)
        return decode_response(json.loads(line), request)
