"""Multiplex MCP and broker RPC over Docker stdio; no published ports or host mounts."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import uuid

from .transport import MAX_FRAME, SOCKET_PATH


async def run() -> None:
    os.umask(0o077)
    pending: dict[str, asyncio.Future] = {}
    output_lock = asyncio.Lock()
    slots = asyncio.Semaphore(16)
    mcp_messages: asyncio.Queue[str] = asyncio.Queue(maxsize=64)

    async def emit(value: dict) -> None:
        async with output_lock:
            sys.stdout.buffer.write(json.dumps(value, separators=(",", ":")).encode() + b"\n")
            sys.stdout.buffer.flush()

    async def fetch(reader, writer):
        try:
            async with slots:
                line = await asyncio.wait_for(reader.readline(), 5)
                if len(line) > 32_768:
                    raise ValueError("Request exceeds policy limit")
                request = json.loads(line)
                identifier = uuid.uuid4().hex
                future = asyncio.get_running_loop().create_future()
                pending[identifier] = future
                try:
                    await emit({"kind": "fetch", "id": identifier, "request": request})
                    result = await asyncio.wait_for(future, 65)
                finally:
                    pending.pop(identifier, None)
                writer.write(json.dumps(result).encode() + b"\n")
                await writer.drain()
        except Exception as exc:
            with contextlib.suppress(Exception):
                writer.write(json.dumps({"error": str(exc)}).encode() + b"\n")
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_unix_server(fetch, SOCKET_PATH, limit=32_769)
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
