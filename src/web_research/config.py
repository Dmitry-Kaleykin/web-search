from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


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


@dataclass(frozen=True, slots=True)
class Settings:
    searxng_url: str = "http://127.0.0.1:8080"
    omlx_base_url: str = "http://127.0.0.1:8000/v1"
    omlx_model: str = ""
    omlx_api_key: str = ""
    data_dir: Path = Path(".web-search-data")
    log_level: str = "INFO"
    user_agent: str = "LocalResearchBot/0.1 (+local personal research)"
    allow_private_urls: bool = False
    max_response_bytes: int = 5_000_000
    document_cache_ttl_seconds: int = 21_600
    search_cache_ttl_seconds: int = 900
    enable_crawl4ai: bool = True
    model_timeout_seconds: float = 90.0
    model_max_tokens: int = 4096
    model_temperature: float = 0.1

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            searxng_url=os.getenv("WEB_SEARCH_SEARXNG_URL", "http://127.0.0.1:8080"),
            omlx_base_url=os.getenv("WEB_SEARCH_OMLX_BASE_URL", "http://127.0.0.1:8000/v1"),
            omlx_model=os.getenv("WEB_SEARCH_OMLX_MODEL", ""),
            omlx_api_key=os.getenv("WEB_SEARCH_OMLX_API_KEY", ""),
            data_dir=Path(os.getenv("WEB_SEARCH_DATA_DIR", ".web-search-data")).expanduser(),
            log_level=os.getenv("WEB_SEARCH_LOG_LEVEL", "INFO").upper(),
            user_agent=os.getenv(
                "WEB_SEARCH_USER_AGENT", "LocalResearchBot/0.1 (+local personal research)"
            ),
            allow_private_urls=_bool_env("WEB_SEARCH_ALLOW_PRIVATE_URLS", False),
            max_response_bytes=_int_env("WEB_SEARCH_MAX_RESPONSE_BYTES", 5_000_000),
            document_cache_ttl_seconds=_int_env("WEB_SEARCH_DOCUMENT_CACHE_TTL_SECONDS", 21_600),
            search_cache_ttl_seconds=_int_env("WEB_SEARCH_SEARCH_CACHE_TTL_SECONDS", 900),
            enable_crawl4ai=_bool_env("WEB_SEARCH_ENABLE_CRAWL4AI", True),
            model_timeout_seconds=_float_env("WEB_SEARCH_MODEL_TIMEOUT_SECONDS", 90.0),
            model_max_tokens=_int_env("WEB_SEARCH_MODEL_MAX_TOKENS", 4096),
            model_temperature=_float_env("WEB_SEARCH_MODEL_TEMPERATURE", 0.1),
        )


@dataclass(frozen=True, slots=True)
class Budget:
    max_seconds: float
    max_searches: int
    max_pages: int
    max_pages_per_domain: int
    checkpoint_every_pages: int
    min_gain: float


BUDGETS: dict[str, Budget] = {
    "quick": Budget(30.0, 2, 5, 2, 1, 0.15),
    "auto": Budget(120.0, 8, 20, 4, 2, 0.08),
    "thorough": Budget(600.0, 20, 60, 8, 3, 0.04),
}


def budget_for(effort: str) -> Budget:
    try:
        return BUDGETS[effort]
    except KeyError as exc:
        raise ValueError(f"Unknown effort level: {effort}") from exc
