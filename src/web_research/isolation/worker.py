"""Multiplex MCP and broker RPC over Docker stdio; no published ports or host mounts."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import uuid

from .transport import BROKER_TIMEOUT_SECONDS, MAX_FRAME, SOCKET_PATH

# One request line per broker connection, plus a bounded POST body and headers.
MAX_REQUEST_LINE = 160_000
# The caller gives up just before its own socket timeout, so the reason it sees is the one this
# worker chose rather than a closed connection.
BROKER_RESULT_TIMEOUT = BROKER_TIMEOUT_SECONDS - 5.0


async def caller_hangup(reader: asyncio.StreamReader) -> None:
    """Return only when the caller closed its end; a stray byte is not a disconnect.

    This runs as a task alongside the wait for a broker result. It must never start before the
    request line has been read, because a competing read on the same stream takes bytes from the
    front of the buffer and would eat the beginning of the request.
    """
    while await reader.read(1):
        continue


async def broker_request(
    reader: asyncio.StreamReader,
    writer,
    *,
    emit,
    pending: dict[str, asyncio.Future],
    slots: asyncio.Semaphore,
) -> None:
    """Carry one broker request for one connection, and release it if the caller disappears.

    The caller can go away while its request waits: an abandoned render, a page that stopped
    caring, a client whose own timeout fired first. Watching for the closed connection is the
    only signal this socket offers, and without it a broker slot and a worker slot both stay held
    by work nobody wants.
    """
    identifier = ""
    answered = False
    reply: dict = {"error": "Native network broker request failed"}
    hangup: asyncio.Task | None = None
    try:
        async with slots:
            line = await asyncio.wait_for(reader.readline(), 5)
            if len(line) > MAX_REQUEST_LINE:
                raise ValueError("Request exceeds policy limit")
            request = json.loads(line)
            identifier = uuid.uuid4().hex
            future: asyncio.Future = asyncio.get_running_loop().create_future()
            pending[identifier] = future
            try:
                # Watching starts here, not at connection setup: ``read(1)`` competes with
                # ``readline()`` for the same buffer, and the request line pays for it.
                hangup = asyncio.create_task(caller_hangup(reader))
                await emit({"kind": "fetch", "id": identifier, "request": request})
                done, _ = await asyncio.wait(
                    (future, hangup),
                    timeout=BROKER_RESULT_TIMEOUT,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if future not in done:
                    raise TimeoutError(
                        "Native network broker connection closed before its result"
                        if hangup in done
                        else "Native network broker timed out"
                    )
                reply = future.result()
                answered = True
            finally:
                pending.pop(identifier, None)
    except Exception as exc:
        reply = {"error": str(exc)}
    finally:
        if not answered and identifier:
            # Release the native queue slot for work nobody is waiting for any more.
            with contextlib.suppress(Exception):
                await emit({"kind": "cancel", "id": identifier})
        with contextlib.suppress(Exception):
            writer.write(json.dumps(reply).encode() + b"\n")
            await writer.drain()
        if hangup is not None:
            hangup.cancel()
            with contextlib.suppress(BaseException):
                await hangup
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def run() -> None:
    os.umask(0o077)
    pending: dict[str, asyncio.Future] = {}
    output_lock = asyncio.Lock()
    # Matches the native queue allowance, so backpressure forms in one place.
    slots = asyncio.Semaphore(16)
    mcp_messages: asyncio.Queue[str] = asyncio.Queue(maxsize=64)

    async def emit(value: dict) -> None:
        async with output_lock:
            sys.stdout.buffer.write(json.dumps(value, separators=(",", ":")).encode() + b"\n")
            sys.stdout.buffer.flush()

    async def fetch(reader, writer):
        await broker_request(reader, writer, emit=emit, pending=pending, slots=slots)

    server = await asyncio.start_unix_server(fetch, SOCKET_PATH, limit=MAX_REQUEST_LINE + 1)
    children = []
    tasks = []
    stdin = asyncio.StreamReader(limit=MAX_FRAME)
    await asyncio.get_running_loop().connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(stdin),
        sys.stdin.buffer,
    )

    async def incoming():
        while line := await stdin.readline():
            frame = json.loads(line)
            if frame.get("kind") == "mcp":
                mcp_messages.put_nowait(frame["data"])
            elif frame.get("kind") == "fetch_result":
                future = pending.get(frame.get("id"))
                if future is not None and not future.done():
                    future.set_result(frame["result"])

    input_task = asyncio.create_task(incoming())
    try:
        search = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "web_research.isolation.searx_worker",
            stdout=sys.stderr,
            stderr=sys.stderr,
        )
        children.append(search)
        for _ in range(300):
            if input_task.done():
                input_task.result()
                return
            if search.returncode is not None:
                raise RuntimeError("Isolated SearXNG exited during startup")
            try:
                _, writer = await asyncio.open_connection("127.0.0.1", 8080)
                writer.close()
                await writer.wait_closed()
                break
            except OSError:
                await asyncio.sleep(0.1)
        else:
            raise RuntimeError("Isolated SearXNG did not start")

        mcp = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "web_research.isolation.mcp_worker",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=sys.stderr,
            limit=16_000_000,
        )
        children.append(mcp)

        async def deliver_mcp():
            while True:
                message = await mcp_messages.get()
                mcp.stdin.write(message.encode() + b"\n")
                await mcp.stdin.drain()

        async def outgoing():
            while line := await mcp.stdout.readline():
                await emit({"kind": "mcp", "data": line.decode().rstrip("\n")})

        tasks = [
            input_task,
            *[
                asyncio.create_task(coro)
                for coro in (deliver_mcp(), outgoing(), search.wait(), mcp.wait())
            ],
        ]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        input_task.cancel()
        server.close()
        await server.wait_closed()
        for future in pending.values():
            if not future.done():
                future.cancel()
        for task in tasks:
            task.cancel()
        for child in reversed(children):
            if child.returncode is None:
                child.terminate()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(child.wait(), 5)
                if child.returncode is None:
                    child.kill()
                    await child.wait()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
