#!/usr/bin/env python3
"""Verify OS isolation, real MCP discovery, page reads, browser rendering and search."""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


class CheckFailure(RuntimeError):
    """An expected failed readiness check, with a user-readable explanation."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailure(message)


def search_ready(search: dict) -> None:
    if search.get("outcome") == "success" and search.get("results"):
        return
    details = [f"Search returned {search.get('outcome', 'an invalid response')}; no results."]
    failures = [
        warning for warning in search.get("warnings", []) if warning.startswith("search_failed:")
    ]
    details.extend(failures or search.get("warnings", [])[:2])
    for engine, reason in search.get("engine_health", {}).items():
        details.append(f"  {engine}: {reason}")
    raise CheckFailure("\n".join(details))


def report_engine_coverage(search: dict) -> None:
    """Name the engines that answered, because an aggregate success can hide a dead fleet.

    A search that returns three hits from one engine looks identical to a search where four
    engines were asked and three were broken, so the acceptance check has to say which one it saw.
    """
    status = search.get("engine_status") or {}
    coverage = search.get("engine_coverage") or {}
    answered = {str(name).casefold() for name in coverage.get("answered", [])}
    for name in coverage.get("requested", []):
        entry = status.get(name) or {}
        reason = f" - {entry['reason']}" if entry.get("reason") else ""
        fault = f" [{entry['fault']}]" if entry.get("fault") else ""
        spoke = " answered" if str(name).casefold() in answered else " silent"
        print(
            f"  engine {name}: {entry.get('status', 'unknown')}{spoke}{reason}{fault}", flush=True
        )
    if coverage.get("attribution") == "absent":
        print("  NOTE the backend gave no per-engine attribution for these results", flush=True)
    broken = sorted(
        name for name, entry in status.items() if entry.get("status") in {"missing", "disabled"}
    )
    require(
        not broken,
        "Configured search engines are not enabled in the SearXNG backend: " + ", ".join(broken),
    )
    untested = sorted(
        name
        for name, entry in status.items()
        if name != "searxng" and entry.get("status") == "untested" and not entry.get("successes")
    )
    if untested:
        print(
            "  NOTE no recorded success since the last backend or policy change: "
            + ", ".join(untested),
            flush=True,
        )


def expected_failures(error: Exception) -> list[str] | None:
    """Unwrap MCP task groups without hiding unrelated programming/transport failures."""
    if isinstance(error, ExceptionGroup):
        messages = []
        for child in error.exceptions:
            nested = expected_failures(child)
            if nested is None:
                return None
            messages.extend(nested)
        return messages
    if isinstance(error, CheckFailure):
        return [str(error)]
    if isinstance(error, subprocess.CalledProcessError):
        return [
            f"Container isolation check exited with status {error.returncode}; see output above."
        ]
    return None


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
        message = "\n".join(item.text for item in result.content if item.type == "text")
        raise CheckFailure(f"MCP request failed: {message}")
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
        require(
            {tool.name for tool in tools.tools} == {"web_search", "read_url"},
            "The isolated worker did not expose both search and reading tools.",
        )
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
        require(
            read.get("title") == "Example Domain" and "documentation" in read.get("content", ""),
            "Public HTTPS reading did not return the expected example page.",
        )
        print("PASS public HTTPS page retrieval through native broker", flush=True)
        blocked = await session.call_tool(
            "read_url",
            {
                "url": "http://169.254.169.254/",
                "render": "never",
                "refresh": True,
            },
        )
        require(blocked.is_error, "Private-address rejection failed; isolation is not ready.")
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
        require(
            rendered.get("extraction_method") == "crawl4ai+chromium",
            "Chromium rendering failed: " + "; ".join(rendered.get("warnings", [])),
        )
        require(
            any(
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
            ),
            "Chromium returned no screenshot.",
        )
        print("PASS real Chromium rendering and screenshot through native broker", flush=True)
        search = result_json(
            await session.call_tool(
                "web_search",
                {
                    "query": "Python documentation",
                    "limit": 3,
                    "refresh": True,
                    # Without this the per-engine evidence is omitted and a one-engine answer
                    # would look identical to a whole fleet answering.
                    "diagnostics": True,
                },
            )
        )
        search_ready(search)
        print("PASS real SearXNG search through native broker", flush=True)
        report_engine_coverage(search)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--debug", action="store_true", help="Show full tracebacks for failed checks"
    )
    args = parser.parse_args(argv)
    try:
        config = json.loads((ROOT / "build/isolation.json").read_text())
        check_container(config)
        asyncio.run(check_mcp())
    except Exception as error:
        messages = expected_failures(error)
        if args.debug or messages is None:
            raise
        for message in messages:
            print(f"FAIL {message}", file=sys.stderr, flush=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
