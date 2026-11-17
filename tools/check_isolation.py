#!/usr/bin/env python3
"""Verify OS isolation, real MCP discovery, page reads, browser rendering and search."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


def check_container(config: dict) -> None:
    probe = r"""
import os, socket
from pathlib import Path
assert os.getuid() == 1000
assert not Path('/var/run/docker.sock').exists()
assert not Path('/Users').exists()
assert not Path('/opt/web-search/.env').exists()
interfaces = Path('/sys/class/net').iterdir()
up = {p.name for p in interfaces if p.is_dir() and int((p/'flags').read_text(),16) & 1}
assert up == {'lo'}, up
assert len(Path('/proc/net/route').read_text().strip().splitlines()) == 1
for address in [('1.1.1.1',443), ('169.254.169.254',80), ('192.168.1.1',80)]:
    try:
        socket.create_connection(address,timeout=1)
    except OSError:
        pass
    else:
        raise AssertionError('Direct network bypass: '+str(address))
try:
    Path('/opt/should-not-write').write_text('test')
except OSError:
    pass
else:
    raise AssertionError('Root filesystem is writable')
print('PASS network namespace, host-file isolation, nonroot user and readonly filesystem')
"""
    subprocess.run(
        [
            config["docker"],
            "--host",
            config["docker_endpoint"],
            "run",
            "--rm",
            "--pull=never",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--user=1000:1000",
            "--entrypoint=python",
            config["image"],
            "-c",
            probe,
        ],
        check=True,
    )


def result_json(result) -> dict:
    if result.is_error:
        raise AssertionError(str(result.content))
    return json.loads(next(item.text for item in result.content if item.type == "text"))


async def check_mcp() -> None:
    params = StdioServerParameters(command=str(ROOT / "web-search-isolated"))
    async with (
        stdio_client(params) as streams,
        ClientSession(
            *streams,
            read_timeout_seconds=120,
        ) as session,
    ):
        await session.initialize()
        tools = await session.list_tools()
        assert {tool.name for tool in tools.tools} == {"web_search", "read_url"}
        print("PASS MCP handshake and tool schemas", flush=True)
        read = result_json(
            await session.call_tool(
                "read_url",
                {
                    "url": "https://example.com",
                    "render": "never",
                    "refresh": True,
                },
            )
        )
        assert read["title"] == "Example Domain" and "documentation" in read["content"], read
        print("PASS public HTTPS page retrieval through native broker", flush=True)
        blocked = await session.call_tool(
            "read_url",
            {
                "url": "http://169.254.169.254/",
                "render": "never",
                "refresh": True,
            },
        )
        assert blocked.is_error, blocked
        print("PASS private-address rejection through MCP", flush=True)
        rendered = result_json(
            await session.call_tool(
                "read_url",
                {
                    "url": "https://example.com",
                    "render": "always",
                    "refresh": True,
                    "visual": True,
                },
            )
        )
        assert rendered["extraction_method"] == "crawl4ai+chromium", rendered
        assert any(
            item.type == "image"
            for item in (
                await session.call_tool(
                    "read_url",
                    {
                        "url": "https://example.com",
                        "render": "always",
                        "visual": True,
                    },
                )
            ).content
        )
        print("PASS real Chromium rendering and screenshot through native broker", flush=True)
        search = result_json(
            await session.call_tool(
                "web_search",
                {
                    "query": "Python documentation",
                    "limit": 3,
                    "refresh": True,
                },
            )
        )
        assert search["outcome"] == "success" and search["results"], search
        print("PASS real SearXNG search through native broker", flush=True)


def main() -> None:
    config = json.loads((ROOT / "build/isolation.json").read_text())
    check_container(config)
    asyncio.run(check_mcp())


if __name__ == "__main__":
    main()
