from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass(slots=True)
class SearchResult:
    url: str
    title: str
    snippet: str = ""
    engines: list[str] = field(default_factory=list)
    published_at: str | None = None
    rank: int = 0
    score: float = 0.0


@dataclass(slots=True)
class Document:
    url: str
    final_url: str
    title: str
    content: str
    method: str
    retrieved_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    published_at: str | None = None
    published_at_source: str | None = None
    content_type: str = "text/html"
    status_code: int | None = None
    warnings: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    images: list[dict[str, Any]] = field(default_factory=list)
    attribution: str | None = None
    available_actions: list[dict[str, str]] = field(default_factory=list)
