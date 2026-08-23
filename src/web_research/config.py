from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)
CONFIG_FILENAME = "config.json"


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value else default


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    return float(value) if value else default


def _string_env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


@dataclass(frozen=True, slots=True)
class Settings:
    searxng_url: str = "http://127.0.0.1:8080"
    model_base_url: str = "http://127.0.0.1:8000/v1"
    model_id: str = ""
    model_api_key: str = ""
    data_dir: Path = Path(".web-search-data")
    log_level: str = "INFO"
    user_agent: str = "LocalResearchBot/0.1 (+local personal research)"
    allow_private_urls: bool = False
    allow_proxy_fake_ips: bool = False
    max_response_bytes: int = 5_000_000
    document_cache_ttl_seconds: int = 21_600
    search_cache_ttl_seconds: int = 900
    enable_crawl4ai: bool = True
    model_timeout_seconds: float = 90.0
    model_max_tokens: int = 4096
    model_temperature: float = 0.1
    evidence_model_base_url: str = ""
    evidence_model_id: str = ""
    evidence_model_api_key: str = ""
    evidence_model_timeout_seconds: float = 90.0
    evidence_model_max_tokens: int = 1600
    evidence_model_temperature: float = 0.1

    @classmethod
    def from_env(cls) -> Settings:
        data_dir = Path(os.getenv("WEB_SEARCH_DATA_DIR", ".web-search-data")).expanduser()
        saved = _read_saved_config(data_dir)
        evidence = saved.get("evidence_model")
        if not isinstance(evidence, dict):
            evidence = {}
        model_base_url = os.getenv(
            "WEB_SEARCH_MODEL_BASE_URL", "http://127.0.0.1:8000/v1"
        )
        model_api_key = os.getenv("WEB_SEARCH_MODEL_API_KEY", "")
        return cls(
            searxng_url=os.getenv("WEB_SEARCH_SEARXNG_URL", "http://127.0.0.1:8080"),
            model_base_url=model_base_url,
            model_id=os.getenv("WEB_SEARCH_MODEL_ID", ""),
            model_api_key=model_api_key,
            data_dir=data_dir,
            log_level=os.getenv("WEB_SEARCH_LOG_LEVEL", "INFO").upper(),
            user_agent=os.getenv(
                "WEB_SEARCH_USER_AGENT", "LocalResearchBot/0.1 (+local personal research)"
            ),
            allow_private_urls=_bool_env("WEB_SEARCH_ALLOW_PRIVATE_URLS", False),
            allow_proxy_fake_ips=_bool_env("WEB_SEARCH_ALLOW_PROXY_FAKE_IPS", False),
            max_response_bytes=_int_env("WEB_SEARCH_MAX_RESPONSE_BYTES", 5_000_000),
            document_cache_ttl_seconds=_int_env("WEB_SEARCH_DOCUMENT_CACHE_TTL_SECONDS", 21_600),
            search_cache_ttl_seconds=_int_env("WEB_SEARCH_SEARCH_CACHE_TTL_SECONDS", 900),
            enable_crawl4ai=_bool_env("WEB_SEARCH_ENABLE_CRAWL4AI", True),
            model_timeout_seconds=_float_env("WEB_SEARCH_MODEL_TIMEOUT_SECONDS", 90.0),
            model_max_tokens=_int_env("WEB_SEARCH_MODEL_MAX_TOKENS", 4096),
            model_temperature=_float_env("WEB_SEARCH_MODEL_TEMPERATURE", 0.1),
            evidence_model_base_url=_string_env(
                "WEB_SEARCH_EVIDENCE_MODEL_BASE_URL",
                _saved_string(evidence, "base_url") or model_base_url,
            ),
            evidence_model_id=_string_env(
                "WEB_SEARCH_EVIDENCE_MODEL_ID", _saved_string(evidence, "model_id")
            ),
            evidence_model_api_key=_string_env(
                "WEB_SEARCH_EVIDENCE_MODEL_API_KEY", model_api_key
            ),
            evidence_model_timeout_seconds=_float_env(
                "WEB_SEARCH_EVIDENCE_MODEL_TIMEOUT_SECONDS", 90.0
            ),
            evidence_model_max_tokens=_int_env(
                "WEB_SEARCH_EVIDENCE_MODEL_MAX_TOKENS", 1600
            ),
            evidence_model_temperature=_float_env(
                "WEB_SEARCH_EVIDENCE_MODEL_TEMPERATURE", 0.1
            ),
        )


def _read_saved_config(data_dir: Path) -> dict[str, Any]:
    path = data_dir / CONFIG_FILENAME
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.warning("Ignoring unreadable saved configuration at %s: %s", path, exc)
        return {}
    if not isinstance(value, dict):
        LOGGER.warning("Ignoring saved configuration at %s because it is not an object", path)
        return {}
    return value


def _saved_string(container: dict[str, Any], key: str) -> str:
    value = container.get(key)
    return str(value).strip() if value is not None else ""


@dataclass(frozen=True, slots=True)
class Budget:
    max_seconds: float
    max_searches: int
    max_pages: int
    max_pages_per_domain: int
    checkpoint_every_pages: int
    min_gain: float
    max_wall_seconds: float | None = None
    synthesis_reserve_seconds: float = 0.0
    max_attempts_per_search_batch: int = 3


BUDGETS: dict[str, Budget] = {
    "quick": Budget(
        30.0,
        2,
        5,
        2,
        1,
        0.15,
        max_wall_seconds=300.0,
        synthesis_reserve_seconds=105.0,
        max_attempts_per_search_batch=2,
    ),
    "auto": Budget(
        120.0,
        8,
        20,
        4,
        2,
        0.08,
        max_wall_seconds=720.0,
        synthesis_reserve_seconds=105.0,
        max_attempts_per_search_batch=3,
    ),
    "thorough": Budget(
        600.0,
        20,
        60,
        8,
        3,
        0.04,
        max_wall_seconds=1500.0,
        synthesis_reserve_seconds=105.0,
        max_attempts_per_search_batch=4,
    ),
}


def budget_for(effort: str) -> Budget:
    try:
        return BUDGETS[effort]
    except KeyError as exc:
        raise ValueError(f"Unknown effort level: {effort}") from exc
