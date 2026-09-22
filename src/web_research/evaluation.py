"""Evaluation entry point: retrieval checks and opt-in caller-model research runs."""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path

from .research_benchmark import call_fixture, describe, load_cases


async def offline_checks() -> dict[str, bool]:
    """Exercise the public MCP tools with fixture search and HTTP responses, without a model.

    These checks validate retrieval and diagnostics. They do not grade research decisions,
    citation support, factual correctness, or whether a model should stop searching.
    """
    cases = {case["id"]: case for case in load_cases() if not case.get("live")}
    checks = {}
    definitions = await describe("current")
    checks["exactly_two_public_tools"] = {tool["name"] for tool in definitions["tools"]} == {
        "web_search",
        "read_url",
    }
    with tempfile.TemporaryDirectory(prefix="web-search-check-") as temporary:
        directory = Path(temporary)
        case = cases["simple_fact"]
        search_args = {"query": case["question"]}
        found = await call_fixture(case, directory / "simple", "web_search", search_args)
        cached = await call_fixture(case, directory / "simple", "web_search", search_args)
        source = case["sources"]["policy"]
        output = found["output"] or {}
        checks["search_returns_source"] = (
            not found["is_error"]
            and output.get("outcome") == "success"
            and any(result["url"] == source["url"] for result in output.get("results", []))
        )
        cached_output = cached["output"] or {}
        checks["cache_preserves_retrieval_time"] = (
            cached_output.get("cache_hit") is True
            and bool(output.get("retrieved_at"))
            and cached_output.get("retrieved_at") == output.get("retrieved_at")
        )
        read = await call_fixture(case, directory / "simple", "read_url", {"url": source["url"]})
        checks["read_returns_source_text"] = not read["is_error"] and source["text"] in (
            read["output"] or {}
        ).get("content", "")
        chosen = await call_fixture(
            case, directory / "simple", "web_search", {**search_args, "engine": "specialist"}
        )
        selected = chosen["output"] or {}
        checks["explicit_engine_selection"] = (
            selected.get("outcome") == "success"
            and bool(selected.get("results"))
            and selected.get("requested_engines") == ["specialist"]
            and all(result["engines"] == ["specialist"] for result in selected.get("results", []))
        )
        invalid = await call_fixture(
            case, directory / "simple", "web_search", {**search_args, "engine": "unknown"}
        )
        checks["unknown_engine_rejected"] = invalid["is_error"]
        blocked = cases["unavailable_sources"]
        failed = await call_fixture(
            blocked, directory / "blocked", "web_search", {"query": blocked["question"]}
        )
        checks["backend_failure_is_not_success"] = (failed["output"] or {}).get(
            "outcome"
        ) == "backend_unavailable"
        unavailable = await call_fixture(
            blocked,
            directory / "blocked",
            "read_url",
            {"url": blocked["sources"]["blocked"]["url"]},
        )
        checks["unavailable_page_is_error"] = unavailable["is_error"]
    return checks


def evaluation_main(argv: list[str] | None = None) -> None:
    arguments = list(sys.argv[1:] if argv is None else argv)
    # Delegate option parsing so each benchmark's help stays authoritative.
    if arguments and arguments[0] == "retrieval":
        from .retrieval_baseline import main

        main(arguments[1:])
        return
    if arguments and arguments[0] == "research":
        from .research_benchmark import main

        main(arguments[1:], run_only=True)
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode",
        nargs="?",
        default="check",
        choices=["check", "retrieval", "research"],
        help="check: offline public-tool checks (default); retrieval: live network baseline; "
        "research: invokes the configured Pi model (may incur provider charges)",
    )
    parser.parse_args(arguments)
    print("Offline public-tool checks: no network or model calls; not a research-quality score.")
    checks = asyncio.run(offline_checks())
    for name, passed in checks.items():
        print(f"{'OK' if passed else 'FAIL'} {name}")
    raise SystemExit(0 if all(checks.values()) else 1)


if __name__ == "__main__":
    evaluation_main()
