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


def test_cookie_lifetime_is_configurable_because_anonimity_is_a_user_choice(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("WEB_SEARCH_COOKIE_TTL_SECONDS=0\n")
    with patch.dict(os.environ, {}, clear=True):
        assert Settings.from_env(env_file=env_file).cookie_ttl_seconds == 0
    with patch.dict(os.environ, {"WEB_SEARCH_COOKIE_TTL_SECONDS": "120"}, clear=True):
        assert Settings.from_env(env_file=env_file).cookie_ttl_seconds == 120


def test_the_challenge_grace_is_bounded_and_can_be_switched_off(tmp_path):
    """A rendered read waits for a self-clearing interstitial; the operator decides how long."""
    missing = tmp_path / "missing.env"
    with patch.dict(os.environ, {}, clear=True):
        assert Settings.from_env(env_file=missing).browser_challenge_grace_seconds == 8.0
    with patch.dict(os.environ, {"WEB_SEARCH_BROWSER_CHALLENGE_GRACE_SECONDS": "0"}, clear=True):
        assert Settings.from_env(env_file=missing).browser_challenge_grace_seconds == 0
    with patch.dict(os.environ, {"WEB_SEARCH_BROWSER_CHALLENGE_GRACE_SECONDS": "-3"}, clear=True):
        assert Settings.from_env(env_file=missing).browser_challenge_grace_seconds == 0
