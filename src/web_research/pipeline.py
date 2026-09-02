from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .agent import heuristic_spec
from .config import BUDGETS, Budget
from .models import TaskType


class SpecStrategy(StrEnum):
    HEURISTIC = "heuristic"
    MODEL = "model"


class QueryStrategy(StrEnum):
    DIRECT = "direct"
    MODEL = "model"


class RerankingStrategy(StrEnum):
    DETERMINISTIC = "deterministic"
    SEMANTIC = "semantic"


class EvidenceStrategy(StrEnum):
    HEURISTIC = "heuristic"
    MODEL = "model"


class FollowupStrategy(StrEnum):
    NONE = "none"
    MODEL = "model"


@dataclass(frozen=True, slots=True)
class PipelineProfile:
    """A mode-independent recipe assembled from reusable research stages."""

    name: str
    budget: Budget
    spec: SpecStrategy
    queries: QueryStrategy
    reranking: RerankingStrategy
    evidence: EvidenceStrategy
    followups: FollowupStrategy
    discover_links: bool
    min_searches: int = 0
    min_usable_domains: int = 0


QUICK_PIPELINE = PipelineProfile(
    name="quick",
    budget=BUDGETS["quick"],
    spec=SpecStrategy.HEURISTIC,
    queries=QueryStrategy.DIRECT,
    reranking=RerankingStrategy.DETERMINISTIC,
    evidence=EvidenceStrategy.HEURISTIC,
    followups=FollowupStrategy.NONE,
    discover_links=False,
)

STANDARD_PIPELINE = PipelineProfile(
    name="standard",
    budget=BUDGETS["auto"],
    spec=SpecStrategy.MODEL,
    queries=QueryStrategy.MODEL,
    reranking=RerankingStrategy.SEMANTIC,
    evidence=EvidenceStrategy.MODEL,
    followups=FollowupStrategy.MODEL,
    discover_links=True,
)

THOROUGH_PIPELINE = PipelineProfile(
    name="thorough",
    budget=BUDGETS["thorough"],
    spec=SpecStrategy.MODEL,
    queries=QueryStrategy.MODEL,
    reranking=RerankingStrategy.SEMANTIC,
    evidence=EvidenceStrategy.MODEL,
    followups=FollowupStrategy.MODEL,
    discover_links=True,
    min_searches=3,
    min_usable_domains=6,
)


def pipeline_for_effort(
    effort: str,
    *,
    inferred_task_type: TaskType | None = None,
) -> PipelineProfile:
    """Resolve a public effort value to a concrete pipeline recipe.

    Auto deliberately promotes only simple facts to the quick path. Broader task types use the
    standard recipe; thorough remains explicit so auto cannot unexpectedly start a long run.
    """
    if effort == "quick":
        return QUICK_PIPELINE
    if effort == "thorough":
        return THOROUGH_PIPELINE
    if effort == "auto":
        return QUICK_PIPELINE if inferred_task_type == TaskType.FACT else STANDARD_PIPELINE
    raise ValueError(f"Unknown effort level: {effort}")


def pipeline_for_request(
    effort: str,
    query: str,
    freshness: str | None,
) -> PipelineProfile:
    routing_spec = heuristic_spec(query, freshness)
    return pipeline_for_effort(effort, inferred_task_type=routing_spec.task_type)
