from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

spec = importlib.util.spec_from_file_location(
    "isolation_check", Path(__file__).resolve().parents[1] / "tools/check_isolation.py"
)
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


def test_search_timeout_reports_engine_reason_without_dumping_results():
    with pytest.raises(check.CheckFailure) as failure:
        check.search_ready(
            {
                "outcome": "backend_unavailable",
                "results": [],
                "warnings": ["search_failed: google cse: timeout"],
                "engine_health": {"google cse": "timeout (119s; cooling_down)"},
            }
        )
    assert "google cse: timeout" in str(failure.value)
    assert "119s" in str(failure.value)
    assert "'results': []" not in str(failure.value)


def test_expected_failure_exits_nonzero_without_nested_python_traceback(tmp_path, capsys):
    (tmp_path / "build").mkdir()
    (tmp_path / "build/isolation.json").write_text(json.dumps({}))
    grouped = ExceptionGroup(
        "MCP task group",
        [ExceptionGroup("nested", [check.CheckFailure("Search unavailable: Google timed out")])],
    )
    with (
        patch.object(check, "ROOT", tmp_path),
        patch.object(check, "check_container"),
        patch.object(check, "check_mcp", AsyncMock(side_effect=grouped)),
        pytest.raises(SystemExit) as exit_status,
    ):
        check.main([])
    assert exit_status.value.code == 1
    stderr = capsys.readouterr().err
    assert "FAIL Search unavailable: Google timed out" in stderr
    assert "Traceback" not in stderr


def test_mixed_groups_preserve_unexpected_failures():
    grouped = ExceptionGroup("mixed", [check.CheckFailure("expected"), ValueError("bug")])
    assert check.expected_failures(grouped) is None


def test_success_requires_results():
    check.search_ready({"outcome": "success", "results": [{"url": "https://example.com"}]})
    with pytest.raises(check.CheckFailure):
        check.search_ready({"outcome": "success", "results": []})
