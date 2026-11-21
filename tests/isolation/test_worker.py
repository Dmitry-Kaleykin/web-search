from __future__ import annotations

import asyncio
import json

from web_research.isolation import worker
from web_research.isolation.worker import MAX_REQUEST_LINE, broker_request


class FakeWriter:
    def __init__(self) -> None:
        self.out = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        self.out += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None

    def replies(self) -> list[dict]:
        return [json.loads(line) for line in self.out.splitlines() if line]


async def drive(*, line: bytes, eof: bool, resolve=None):
    """Serve one connection the way it happens live: handler first, bytes a moment later.

    Feeding the request before the handler starts hides a real race. A second read on a stream
    whose buffer is already full takes bytes off the front of the request line.
    """
    reader = asyncio.StreamReader(limit=MAX_REQUEST_LINE + 1)
    writer = FakeWriter()
    emitted: list[dict] = []
    pending: dict[str, asyncio.Future] = {}

    async def emit(value: dict) -> None:
        emitted.append(value)
        if resolve is not None and value["kind"] == "fetch":
            resolve(value, pending)

    task = asyncio.create_task(
        broker_request(reader, writer, emit=emit, pending=pending, slots=asyncio.Semaphore(2))
    )
    await asyncio.sleep(0)
    reader.feed_data(line)
    if eof:
        reader.feed_eof()
    await task
    return writer, emitted, pending


async def test_the_request_line_arrives_whole():
    """A disconnect watcher started too early reads the first byte of the request itself."""
    payload = {"url": "https://example.com", "method": "GET", "headers": {"accept": "*/*"}}

    def resolve(value, pending):
        assert value["request"] == payload
        pending[value["id"]].set_result({"status": 204, "headers": [], "body": ""})

    writer, emitted, _ = await drive(
        line=json.dumps(payload).encode() + b"\n", eof=False, resolve=resolve
    )
    assert emitted == [{"kind": "fetch", "id": emitted[0]["id"], "request": payload}]
    assert writer.replies() == [{"status": 204, "headers": [], "body": ""}]


async def test_a_result_is_returned_to_the_connection_that_asked_for_it():
    payload = {"url": "https://example.com", "method": "GET", "headers": {}}

    def resolve(value, pending):
        pending[value["id"]].set_result({"status": 200, "headers": [], "body": ""})

    writer, emitted, pending = await drive(
        line=json.dumps(payload).encode() + b"\n", eof=False, resolve=resolve
    )
    assert [frame["kind"] for frame in emitted] == ["fetch"]
    assert writer.replies() == [{"status": 200, "headers": [], "body": ""}]
    assert pending == {}
    assert writer.closed


async def test_a_caller_that_disconnects_cancels_its_request_at_the_broker():
    """Without the cancel frame the broker keeps a slot for a result nobody will ever read."""
    line = json.dumps({"url": "https://example.com/slow", "method": "GET"}).encode() + b"\n"
    writer, emitted, pending = await drive(line=line, eof=True)
    assert [frame["kind"] for frame in emitted] == ["fetch", "cancel"]
    assert emitted[1]["id"] == emitted[0]["id"]
    assert pending == {}
    assert "closed before its result" in writer.replies()[0]["error"]


async def test_an_oversized_request_line_is_refused_without_crossing_the_boundary():
    line = b'{"url": "' + b"x" * (MAX_REQUEST_LINE + 10) + b'"}\n'
    writer, emitted, _ = await drive(line=line, eof=True)
    assert emitted == []
    assert "error" in writer.replies()[0]


def test_the_worker_gives_up_before_the_transport_does():
    # The transport reports its own timeout; the worker only catches requests that outran it.
    assert worker.BROKER_RESULT_TIMEOUT < worker.BROKER_TIMEOUT_SECONDS
    # A request line carries a bounded POST body plus headers.
    assert MAX_REQUEST_LINE > 64 * 1024
