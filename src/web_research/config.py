from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

LOGGER = logging.getLogger(__name__)
PROJECT_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"


def _bool_env(environment: Mapping[str, str], name: str, default: bool) -> bool:
    value = environment.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(environment: Mapping[str, str], name: str, default: int) -> int:
    value = environment.get(name)
    return int(value) if value else default


def _float_env(environment: Mapping[str, str], name: str, default: float) -> float:
    value = environment.get(name)
    return float(value) if value else default


def _string_env(environment: Mapping[str, str], name: str, default: str = "") -> str:
    value = environment.get(name)
    return value.strip() if value and value.strip() else default


@dataclass(frozen=True, slots=True)
class Settings:
    searxng_url: str = "http://127.0.0.1:8080"
    data_dir: Path = Path(".web-search-data")
    log_level: str = "INFO"
    user_agent: str = "LocalResearchBot/0.1 (+local personal research)"
    allow_private_urls: bool = False
    allow_proxy_fake_ips: bool = False
    max_response_bytes: int = 5_000_000
    document_max_chars: int = 1_000_000
    browser_max_concurrent_renders: int = 2
    cache_search_max_rows: int = 2_000
    cache_document_max_rows: int = 400
    cache_document_max_payload_bytes: int = 2_000_000
    document_cache_ttl_seconds: int = 21_600
    search_cache_ttl_seconds: int = 900
    enable_crawl4ai: bool = True
    read_url_max_chars: int = 60_000
    read_url_max_links: int = 100
    search_timeout_seconds: float = 30.0
    search_healthy_engines: str = "google cse,duckduckgo web,mwmbl,searchmysite,mojeek,crowdview"

    @classmethod
    def from_env(cls, *, env_file: Path | None = None) -> Settings:
        environment = _merged_environment(env_file or PROJECT_ENV_PATH)
        data_dir = Path(environment.get("WEB_SEARCH_DATA_DIR", ".web-search-data")).expanduser()
        return cls(
            searxng_url=environment.get("WEB_SEARCH_SEARXNG_URL", "http://127.0.0.1:8080"),
            data_dir=data_dir,
            log_level=environment.get("WEB_SEARCH_LOG_LEVEL", "INFO").upper(),
            user_agent=environment.get(
                "WEB_SEARCH_USER_AGENT", "LocalResearchBot/0.1 (+local personal research)"
            ),
            allow_private_urls=_bool_env(environment, "WEB_SEARCH_ALLOW_PRIVATE_URLS", False),
            allow_proxy_fake_ips=_bool_env(environment, "WEB_SEARCH_ALLOW_PROXY_FAKE_IPS", False),
            max_response_bytes=_int_env(environment, "WEB_SEARCH_MAX_RESPONSE_BYTES", 5_000_000),
            document_max_chars=_int_env(environment, "WEB_SEARCH_DOCUMENT_MAX_CHARS", 1_000_000),
            browser_max_concurrent_renders=_int_env(
                environment, "WEB_SEARCH_BROWSER_MAX_CONCURRENT_RENDERS", 2
            ),
            cache_search_max_rows=_int_env(environment, "WEB_SEARCH_CACHE_SEARCH_MAX_ROWS", 2_000),
            cache_document_max_rows=_int_env(
                environment, "WEB_SEARCH_CACHE_DOCUMENT_MAX_ROWS", 400
            ),
            cache_document_max_payload_bytes=_int_env(
                environment, "WEB_SEARCH_CACHE_DOCUMENT_MAX_PAYLOAD_BYTES", 2_000_000
            ),
            document_cache_ttl_seconds=_int_env(
                environment, "WEB_SEARCH_DOCUMENT_CACHE_TTL_SECONDS", 21_600
            ),
            search_cache_ttl_seconds=_int_env(
                environment, "WEB_SEARCH_SEARCH_CACHE_TTL_SECONDS", 900
            ),
            enable_crawl4ai=_bool_env(environment, "WEB_SEARCH_ENABLE_CRAWL4AI", True),
            read_url_max_chars=max(
                1, _int_env(environment, "WEB_SEARCH_READ_URL_MAX_CHARS", 60_000)
            ),
            read_url_max_links=max(0, _int_env(environment, "WEB_SEARCH_READ_URL_MAX_LINKS", 100)),
            search_timeout_seconds=max(
                1.0, _float_env(environment, "WEB_SEARCH_SEARCH_TIMEOUT_SECONDS", 30.0)
            ),
            search_healthy_engines=_string_env(
                environment,
                "WEB_SEARCH_SEARCH_HEALTHY_ENGINES",
                "google cse,duckduckgo web,mwmbl,searchmysite,mojeek,crowdview",
            ),
        )


def _merged_environment(env_file: Path) -> dict[str, str]:
    """Load project defaults without overriding the MCP process environment."""
    return {**_read_env_file(env_file), **os.environ}


def _read_env_file(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        LOGGER.warning("Ignoring unreadable environment file at %s: %s", path, exc)
        return {}

    values: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values
