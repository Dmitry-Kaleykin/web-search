from unittest.mock import AsyncMock, patch

import pytest

from web_research.evaluation import evaluation_main, offline_checks


async def test_offline_checks_exercise_tools_without_network_or_model():
    # Fixture boundaries must intercept all I/O. Fail if a new path escapes them.
    with (
        patch("httpx.AsyncClient.send", side_effect=AssertionError("unexpected network")),
        patch("subprocess.Popen", side_effect=AssertionError("unexpected model process")),
    ):
        checks = await offline_checks()
    assert checks
    assert all(checks.values()), checks


@pytest.mark.parametrize("passed,exit_code", [(True, 0), (False, 1)])
def test_default_console_check_propagates_failure(passed, exit_code, capsys):
    with (
        patch("web_research.evaluation.offline_checks", AsyncMock(return_value={"read": passed})),
        pytest.raises(SystemExit) as result,
    ):
        evaluation_main([])
    assert result.value.code == exit_code
    assert ("OK read" if passed else "FAIL read") in capsys.readouterr().out


def test_research_mode_is_explicit_and_forwards_options():
    with patch("web_research.research_benchmark.main") as main:
        evaluation_main(["research", "--case", "simple_fact", "--directory", "/tmp/benchmark"])
    main.assert_called_once_with(
        ["--case", "simple_fact", "--directory", "/tmp/benchmark"], run_only=True
    )


def test_research_help_describes_public_command_and_model_use(capsys):
    with pytest.raises(SystemExit) as result:
        evaluation_main(["research", "--help"])
    assert result.value.code == 0
    help_text = capsys.readouterr().out
    assert "web-search-eval research" in help_text
    assert "normal billing" in help_text
    assert "{describe,call,run}" not in help_text


def test_retrieval_mode_forwards_options():
    with patch("web_research.retrieval_baseline.main") as main:
        evaluation_main(["retrieval", "--output", "/tmp/retrieval.json"])
    main.assert_called_once_with(["--output", "/tmp/retrieval.json"])
