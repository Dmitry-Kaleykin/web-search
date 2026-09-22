from __future__ import annotations

import os
from unittest.mock import patch

from web_research.config import Settings


def test_process_environment_overrides_project_defaults(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("WEB_SEARCH_LOG_LEVEL=DEBUG\nWEB_SEARCH_READ_URL_MAX_CHARS=12345\n")
    with patch.dict(os.environ, {"WEB_SEARCH_LOG_LEVEL": "WARNING"}, clear=True):
        settings = Settings.from_env(env_file=env_file)
    assert settings.log_level == "WARNING"
    assert settings.read_url_max_chars == 12345


def test_retired_model_and_stopping_settings_are_ignored(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "WEB_SEARCH_MODEL_MAX_TOKENS=obsolete\n"
        "WEB_SEARCH_RERANKER_MIN_RELEVANCE_SCORE=obsolete\n"
        "WEB_SEARCH_SEARCH_MAX_RETRIES=obsolete\n"
        "WEB_SEARCH_PREFETCH_PAGES=obsolete\n"
        "WEB_SEARCH_READ_URL_MAX_LINKS=17\n"
    )
    with patch.dict(os.environ, {}, clear=True):
        settings = Settings.from_env(env_file=env_file)
    assert settings.read_url_max_links == 17
    assert not hasattr(settings, "model_id")
    assert not hasattr(settings, "reranker_model_id")
    assert not hasattr(settings, "search_max_retries")


def test_active_resource_limits_are_bounded(tmp_path):
    with patch.dict(
        os.environ,
        {
            "WEB_SEARCH_READ_URL_MAX_CHARS": "0",
            "WEB_SEARCH_READ_URL_MAX_LINKS": "-1",
            "WEB_SEARCH_SEARCH_TIMEOUT_SECONDS": "0",
        },
        clear=True,
    ):
        settings = Settings.from_env(env_file=tmp_path / "missing.env")
    assert settings.read_url_max_chars == 1
    assert settings.read_url_max_links == 0
    assert settings.search_timeout_seconds == 1
