from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class TaskType(StrEnum):
    FACT = "fact"
    EXPLANATION = "explanation"
    COMPARISON = "comparison"
    RECOMMENDATION = "recommendation"
    CURRENT_EVENT = "current_event"
    EXPLORATION = "exploration"


class Importance(StrEnum):
    REQUIRED = "required"
    IMPORTANT = "important"
    OPTIONAL = "optional"


class SourceClass(StrEnum):
    PRIMARY = "primary"
    EXPERT = "expert"
    INDEPENDENT = "independent"
    NEWS = "news"
    COMMUNITY = "community"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class Requirement:
    id: str
    question: str
    importance: Importance = Importance.REQUIRED
    subject: str | None = None
    criterion: str | None = None
    min_sources: int = 1
    primary_required: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any], index: int) -> Requirement:
        importance_value = str(data.get("importance", "required"))
        try:
            importance = Importance(importance_value)
        except ValueError:
            importance = Importance.REQUIRED
        return cls(
            id=str(data.get("id") or f"R{index}"),
            question=str(data.get("question") or data.get("description") or "").strip(),
            importance=importance,
            subject=_optional_str(data.get("subject")),
            criterion=_optional_str(data.get("criterion")),
            min_sources=max(1, min(3, int(data.get("min_sources", 1)))),
            primary_required=bool(data.get("primary_required", False)),
        )


@dataclass(slots=True)
class ResearchSpec:
    original_query: str
    task_type: TaskType
    requirements: list[Requirement]
    subjects: list[str] = field(default_factory=list)
    answer_format: str = "A concise, well-supported answer with inline source references."
    locale: str | None = None
    freshness: str | None = None

    def required_requirements(self) -> list[Requirement]:
        return [item for item in self.requirements if item.importance == Importance.REQUIRED]


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
    content_type: str = "text/html"
    warnings: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Source:
    id: str
    url: str
    title: str
    domain: str
    source_class: SourceClass
    retrieved_at: str
    published_at: str | None = None
    extraction_method: str = "http"
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Claim:
    id: str
    requirement_id: str
    source_id: str
    statement: str
    excerpt: str
    confidence: float = 0.5
    stance: str = "supports"


@dataclass(slots=True)
class CoverageItem:
    requirement_id: str
    covered: bool
    source_count: int
    has_primary: bool
    reason: str


@dataclass(slots=True)
class CoverageReport:
    score: float
    sufficient: bool
    items: list[CoverageItem]
    unresolved_gaps: list[str]
    conflicts: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ResearchStats:
    search_queries: int = 0
    pages_fetched: int = 0
    independent_domains: int = 0
    elapsed_ms: int = 0
    cache_hits: int = 0
    fetch_failures: int = 0


@dataclass(slots=True)
class ResearchResult:
    research_id: str
    answer_markdown: str
    sources: list[Source]
    coverage: CoverageReport
    stop_reason: str
    stats: ResearchStats
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
