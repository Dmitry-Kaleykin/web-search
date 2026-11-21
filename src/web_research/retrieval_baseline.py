"""Opt-in live retrieval recording through the public MCP tools, without a model judge."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import platform
import shutil
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path

from mcp.client import Client
from mcp.client.stdio import StdioServerParameters, stdio_client


def _json_bytes(value) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


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
            "output_bytes": _json_bytes(result.structured_content),
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
            "output_bytes": 0,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _launcher_path(launcher: Path | None) -> Path:
    if launcher is None:
        local = Path(__file__).resolve().parents[2] / "web-search-isolated"
        installed = shutil.which("web-search-isolated")
        launcher = local if local.is_file() else Path(installed) if installed else None
    if launcher is None or not launcher.is_file():
        raise ValueError(
            "Isolated launcher not found; pass --launcher /absolute/path/web-search-isolated"
        )
    return launcher.resolve()


async def record_baseline(
    cases: list[dict], *, launcher: Path | None = None, label: str | None = None
) -> dict:
    """Record results and bounded cache/continuation checks; never grade answer quality."""
    launcher = _launcher_path(launcher)
    report = {
        "recorded_at": datetime.now(UTC).isoformat(),
        "scope": "Isolated live retrieval; relevance and missing passages require manual review.",
        "label": label,
        "environment": {
            "client_python": platform.python_version(),
            "transport": "isolated_stdio",
            "launcher": str(launcher),
        },
        "cases_sha256": hashlib.sha256(
            json.dumps(cases, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest(),
        "cases": [],
    }
    manifest = launcher.parent / "build/isolation.json"
    if manifest.is_file():
        report["environment"]["image"] = json.loads(manifest.read_text()).get("image")
    async with Client(
        stdio_client(StdioServerParameters(command=str(launcher))), mode="auto"
    ) as client:
        definitions = await client.list_tools()
        report["environment"]["tools_sha256"] = hashlib.sha256(
            json.dumps(
                [tool.model_dump(mode="json") for tool in definitions.tools], sort_keys=True
            ).encode()
        ).hexdigest()
        # Keep calls sequential so the baseline does not manufacture upstream rate pressure.
        # Cache follow-ups use the same worker and preserve the original retrieval conditions.
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
                if case.get("access_probe"):
                    # A blocked probe is an access observation, never successful content.
                    checks["access_status_reported"] = call["is_error"] or output.get(
                        "page_status"
                    ) in {"ok", "incomplete", "suspected_error"}
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
                if case.get("expected_method"):
                    checks["expected_method_used"] = (
                        output.get("extraction_method") == case["expected_method"]
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
                    "category": case.get("category", "unspecified"),
                    "review": case.get("review"),
                    "manual_review": {
                        "relevant_results_at_5": None,
                        "missing_passages": None,
                        "notes": None,
                    },
                    "observation": {
                        "outcome": output.get("outcome"),
                        "page_status": output.get("page_status"),
                        "result_count": len(output.get("results", [])),
                        "returned_characters": len(output.get("content", "")),
                        "extracted_characters": output.get("content_characters"),
                    },
                    "call": call,
                    "followups": followups,
                    "checks": checks,
                }
            )
    calls = [case["call"] for case in report["cases"]]
    cached = [
        followup
        for case in report["cases"]
        for followup in case["followups"]
        if (followup["output"] or {}).get("cache_hit")
    ]
    report["summary"] = {
        "cases": len(calls),
        "failed_cases": sum(not all(case["checks"].values()) for case in report["cases"]),
        "median_elapsed_ms": statistics.median(call["elapsed_ms"] for call in calls)
        if calls
        else None,
        "median_cache_elapsed_ms": (
            statistics.median(call["elapsed_ms"] for call in cached) if cached else None
        ),
        "output_bytes": sum(call["output_bytes"] for call in calls),
        "access_limited_cases": [
            case["id"]
            for case in report["cases"]
            if case["call"]["is_error"]
            or case["observation"]["outcome"] == "backend_unavailable"
            or case["observation"]["page_status"] in {"incomplete", "suspected_error"}
        ],
    }
    return report


def compare_reports(previous: dict, current: dict) -> list[dict]:
    """Compare identical requests; changing a corpus must not fabricate improvements."""
    old = {case["id"]: case for case in previous["cases"]}
    comparison = []
    for case in current["cases"]:
        prior = old.get(case["id"])
        if prior is None:
            comparison.append({"id": case["id"], "comparable": False, "reason": "new case"})
            continue
        comparable = (
            prior["call"]["tool"] == case["call"]["tool"]
            and prior["call"]["arguments"] == case["call"]["arguments"]
        )
        if not comparable:
            comparison.append({"id": case["id"], "comparable": False, "reason": "request changed"})
            continue
        comparison.append(
            {
                "id": case["id"],
                "comparable": True,
                "elapsed_ms_delta": case["call"]["elapsed_ms"] - prior["call"]["elapsed_ms"],
                "output_bytes_delta": _json_bytes(case["call"]["output"])
                - _json_bytes(prior["call"]["output"]),
                "previous_checks_passed": all(prior["checks"].values()),
                "current_checks_passed": all(case["checks"].values()),
            }
        )
    return comparison


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cases",
        type=Path,
        default=Path(__file__).with_name("benchmark_data") / "retrieval_cases.json",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--launcher", type=Path, help="Isolated launcher; defaults to this checkout or PATH"
    )
    parser.add_argument("--label", help="Name this runtime/provider configuration in the report")
    parser.add_argument(
        "--case", action="append", dest="case_ids", help="Run selected case IDs (repeatable)"
    )
    parser.add_argument(
        "--compare", type=Path, help="Add per-request deltas against a saved report"
    )
    args = parser.parse_args(argv)
    cases = json.loads(args.cases.read_text())
    if args.case_ids:
        unknown = set(args.case_ids) - {case["id"] for case in cases}
        if unknown:
            parser.error(f"unknown case IDs: {', '.join(sorted(unknown))}")
        cases = [case for case in cases if case["id"] in args.case_ids]
    try:
        report = asyncio.run(record_baseline(cases, launcher=args.launcher, label=args.label))
    except ValueError as exc:
        parser.error(str(exc))
    if args.compare:
        report["comparison"] = compare_reports(json.loads(args.compare.read_text()), report)
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
