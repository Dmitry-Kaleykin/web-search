"""Opt-in live retrieval recording through the public MCP tools, without a model judge."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import platform
import time
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

from mcp.client import Client

from .config import Settings
from .server import close_reader_runtimes, mcp


async def _call(client, tool: str, arguments: dict) -> dict:
    started = time.monotonic()
    try:
        result = await client.call_tool(tool, arguments)
        return {
            "tool": tool,
            "arguments": arguments,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "is_error": result.is_error,
            "output": result.structured_content,
            "error": "\n".join(getattr(item, "text", "") for item in result.content)
            if result.is_error
            else None,
        }
    except Exception as exc:
        return {
            "tool": tool,
            "arguments": arguments,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "is_error": True,
            "output": None,
            "error": f"{type(exc).__name__}: {exc}",
        }


async def record_baseline(cases: list[dict]) -> dict:
    """Record results and bounded cache/continuation checks; never grade answer quality."""
    settings = Settings.from_env()
    report = {
        "recorded_at": datetime.now(UTC).isoformat(),
        "scope": "Live retrieval mechanics only; search relevance requires source review.",
        "environment": {
            "python": platform.python_version(),
            "trafilatura": version("trafilatura"),
            "mcp": version("mcp"),
            "engines": settings.search_healthy_engines,
            "allow_proxy_fake_ips": settings.allow_proxy_fake_ips,
            "browser_enabled": settings.enable_crawl4ai,
        },
        "cases_sha256": hashlib.sha256(
            json.dumps(cases, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest(),
        "cases": [],
    }
    try:
        async with Client(mcp) as client:
            for case in cases:
                call = await _call(client, case["tool"], case["arguments"])
                output = call["output"] or {}
                checks = {"tool_completed": not call["is_error"]}
                followups = []
                if case["tool"] == "web_search":
                    checks["results_available"] = output.get("outcome") == "success"
                    checks["retrieval_time_present"] = bool(output.get("retrieved_at"))
                    if checks["results_available"]:
                        cached = await _call(
                            client,
                            "web_search",
                            {
                                **case["arguments"],
                                "refresh": False,
                            },
                        )
                        followups.append(cached)
                        cached_output = cached["output"] or {}
                        checks["cache_provenance_preserved"] = (
                            not cached["is_error"]
                            and cached_output.get("cache_hit") is True
                            and cached_output.get("retrieved_at") == output.get("retrieved_at")
                            and cached_output.get("warnings") == output.get("warnings")
                            and cached_output.get("results") == output.get("results")
                        )
                else:
                    checks["page_ok"] = output.get("page_status") == "ok"
                    if case.get("expected_text"):
                        checks["expected_text_present"] = case["expected_text"] in output.get(
                            "content", ""
                        )
                    if case.get("expected_markdown_target"):
                        checks["relative_link_preserved"] = (
                            f"]({case['expected_markdown_target']})" in output.get("content", "")
                        )
                    if output.get("next_cursor"):
                        continued = await _call(
                            client,
                            "read_url",
                            {
                                "url": case["arguments"]["url"],
                                "cursor": output["next_cursor"],
                                "max_chars": case["arguments"].get("max_chars", 4000),
                            },
                        )
                        followups.append(continued)
                        next_output = continued["output"] or {}
                        checks["continuation_preserved"] = (
                            not continued["is_error"]
                            and next_output.get("snapshot_id") == output.get("snapshot_id")
                            and next_output.get("retrieved_at") == output.get("retrieved_at")
                            and next_output.get("content_start") == output.get("content_end")
                        )
                report["cases"].append(
                    {
                        "id": case["id"],
                        "review": case.get("review"),
                        "call": call,
                        "followups": followups,
                        "checks": checks,
                    }
                )
    finally:
        await close_reader_runtimes()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=Path("eval/retrieval_cases.json"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cases = json.loads(args.cases.read_text())
    report = asyncio.run(record_baseline(cases))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    failed = False
    for case in report["cases"]:
        failures = [name for name, passed in case["checks"].items() if not passed]
        failed |= bool(failures)
        print(
            f"{'FAIL' if failures else 'OK'} {case['id']}: "
            f"{', '.join(failures) if failures else 'retrieval checks passed'}"
        )
    print(f"Recorded {args.output}; inspect the results before judging relevance.")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
