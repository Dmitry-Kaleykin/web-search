"""HTTPX transport over the worker's local Unix socket, never an internet socket."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import socket

import httpx

SOCKET_PATH = "/tmp/web-search-broker.sock"
MAX_FRAME = 8_000_000
# Mirror of the native policy, so a caller gets a local explanation before the round trip.
MAX_REQUEST_BODY_BYTES = 64 * 1024
MAX_BROKER_RESPONSE_BYTES = 5_000_000
POST_CONTENT_TYPES = {"application/x-www-form-urlencoded", "application/json", "text/plain"}
# Bump when the native request policy changes: allowed methods, forwarded header names, cookie
# handling, or the body and response budgets. Engine cooldowns are evidence about this policy as
# much as about a website, and a rebuilt image with an identical SearXNG version must not keep
# engines parked for a fault that the new policy removed.
POLICY_ID = "2026-11-21"
# The broker queues briefly, then allows a connect and a full transfer. Waiting less than that
# would report a timeout for a request the broker is still working on.
BROKER_TIMEOUT_SECONDS = 75.0


def isolated() -> bool:
    return os.environ.get("WEB_SEARCH_ISOLATED_WORKER") == "1"


class BrokerFailure(httpx.TransportError):
    """A request the native broker refused or could not complete, with a machine-readable kind.

    ``kind`` is one of ``policy`` (the request itself is not allowed), ``too_large``,
    ``cancelled`` or ``transport``. Callers use it to avoid blaming a website for a local
    decision, and to avoid escalating a request a browser could not do better on.
    """

    def __init__(self, message: str, *, kind: str, request: httpx.Request | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        if request is not None:
            self.request = request


def _sync_body(request: httpx.Request) -> bytes:
    try:
        return request.read()
    except (httpx.StreamError, RuntimeError):
        # A request that cannot show its body is treated as bodyless. `request_payload` then
        # rejects anything that would need one, so nothing is sent that was never verified.
        return b""


async def request_body(request: httpx.Request) -> bytes:
    """Collect a request body without assuming it is replayable.

    SearXNG hands over an unread async stream even for requests that carry no body, and
    ``Request.read()`` refuses those. Reading is bounded, because the limit is enforced here and
    not by whatever produced the stream.
    """
    if isinstance(request.stream, httpx.AsyncByteStream):
        collected = bytearray()
        async for chunk in request.stream:
            collected.extend(chunk)
            if len(collected) > MAX_REQUEST_BODY_BYTES:
                break
        return bytes(collected)
    return _sync_body(request)


def request_payload(request: httpx.Request, body: bytes = b"") -> dict:
    """Build the broker request for an httpx request, or raise before it leaves the container."""
    if request.method not in {"GET", "HEAD", "POST"}:
        raise httpx.UnsupportedProtocol("Web Search blocks upload/write methods", request=request)
    if request.method == "POST":
        if len(body) > MAX_REQUEST_BODY_BYTES:
            raise httpx.RequestError(
                f"Request body exceeds the {MAX_REQUEST_BODY_BYTES} byte POST limit",
                request=request,
            )
        content_type = dict(request.headers).get("content-type", "")
        if content_type.split(";")[0].strip().lower() not in POST_CONTENT_TYPES:
            raise httpx.RequestError(
                "Network broker policy: POST requires a form, JSON or text body", request=request
            )
    elif body:
        raise httpx.UnsupportedProtocol(
            "Web Search blocks request bodies on GET and HEAD", request=request
        )
    if dict(request.headers).get("authorization"):
        raise httpx.RequestError(
            "Network broker policy: credentials stay outside the container", request=request
        )
    payload: dict = {
        "url": str(request.url),
        "method": request.method,
        "headers": dict(request.headers),
    }
    if body:
        # The native side enforces the same limit and rejects a NUL byte.
        payload["body"] = body.decode("utf-8", "surrogateescape")
    return payload


def decode_response(result: dict, request: httpx.Request) -> httpx.Response:
    if "error" in result:
        raise BrokerFailure(
            str(result["error"]), kind=str(result.get("error_kind", "transport")), request=request
        )
    extensions: dict = {}
    if filtered := result.get("filtered"):
        extensions["broker_filtered"] = filtered
    if forwarded := result.get("forwarded"):
        extensions["broker_forwarded"] = forwarded
    return httpx.Response(
        int(result["status"]),
        headers=result["headers"],
        content=base64.b64decode(result["body"], validate=True),
        request=request,
        extensions=extensions,
    )


def _wait_seconds(request: httpx.Request) -> float:
    """Honour the caller's own read timeout, capped by the broker's transfer budget.

    httpx passes its effective timeout into the transport instead of enforcing it there, so a
    custom transport that ignores it would keep a search engine or a page render waiting long
    after the caller had given up. Closing here is what lets the worker tell the broker the
    result is no longer wanted.
    """
    declared = (request.extensions.get("timeout") or {}).get("read")
    try:
        # A missing or infinite caller timeout defers to the broker's own transfer budget.
        value = float(declared) if declared else BROKER_TIMEOUT_SECONDS
    except (TypeError, ValueError):
        value = BROKER_TIMEOUT_SECONDS
    return min(BROKER_TIMEOUT_SECONDS, max(0.01, value))


class BrokerTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        payload = request_payload(request, await request_body(request))
        reader, writer = await asyncio.open_unix_connection(SOCKET_PATH, limit=160_000)
        try:
            writer.write(json.dumps(payload).encode() + b"\n")
            await writer.drain()
            deadline = _wait_seconds(request)
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=deadline)
            except TimeoutError as exc:
                # Whose deadline expired matters: this one belongs to the caller, and the site
                # was never told about the request at all.
                raise httpx.ReadTimeout(
                    f"Network broker did not answer within {deadline:.1f}s", request=request
                ) from exc
            if not line or len(line) > MAX_FRAME:
                raise httpx.ConnectError("Native network broker disconnected", request=request)
            return decode_response(decode_reply(line, request), request)
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


def decode_reply(line: bytes, request: httpx.Request) -> dict:
    """Parse one broker reply frame, naming the bytes when the framing has come apart.

    A bare ``Extra data: line 1 column 6`` from ``json.loads`` describes neither who wrote the
    frame nor what was in it, which makes a framing bug look like a website problem.
    """
    try:
        return json.loads(line)
    except ValueError as exc:
        preview = line[:160].decode("utf-8", "replace")
        raise httpx.ConnectError(
            f"Unparseable broker reply ({exc}, {len(line)} bytes): {preview!r}", request=request
        ) from exc


class SyncBrokerTransport(httpx.BaseTransport):
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        payload = request_payload(request, _sync_body(request))
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(BROKER_TIMEOUT_SECONDS)
            connection.connect(SOCKET_PATH)
            connection.sendall(json.dumps(payload).encode() + b"\n")
            try:
                with connection.makefile("rb") as stream:
                    line = stream.readline(MAX_FRAME + 1)
            except TimeoutError as exc:
                raise httpx.ReadTimeout(
                    f"Network broker did not answer within {BROKER_TIMEOUT_SECONDS:.1f}s",
                    request=request,
                ) from exc
            if not line or len(line) > MAX_FRAME:
                raise httpx.ConnectError("Native network broker disconnected", request=request)
        return decode_response(decode_reply(line, request), request)
