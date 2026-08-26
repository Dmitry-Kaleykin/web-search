from __future__ import annotations

from collections import Counter

from .models import ResearchSpec, SearchResult
from .safety.urls import registrable_domain
from .text import lexical_similarity


def rank_candidates(
    candidates: list[SearchResult],
    spec: ResearchSpec,
    uncovered_requirement_ids: list[str],
    domain_counts: Counter[str],
    semantic_scores: dict[str, float] | None = None,
) -> list[tuple[float, SearchResult]]:
    uncovered = [
        item for item in spec.requirements if item.id in set(uncovered_requirement_ids)
    ] or spec.requirements
    ranked: list[tuple[float, SearchResult]] = []
    for candidate in candidates:
        candidate_text = f"{candidate.title} {candidate.snippet}"
        relevance = max(
            (lexical_similarity(candidate_text, item.question) for item in uncovered),
            default=0.0,
        )
        result_rank = 1.0 / max(1, candidate.rank)
        engine_bonus = min(0.15, 0.03 * len(set(candidate.engines)))
        domain = registrable_domain(candidate.url)
        diversity = 1.0 / (1.0 + domain_counts[domain])
        if semantic_scores is None or candidate.url not in semantic_scores:
            score = 0.58 * relevance + 0.18 * result_rank + 0.16 * diversity + engine_bonus
        else:
            semantic = max(0.0, min(1.0, semantic_scores[candidate.url]))
            score = (
                0.46 * semantic
                + 0.32 * relevance
                + 0.1 * result_rank
                + 0.08 * diversity
                + min(0.04, engine_bonus)
            )
        ranked.append((score, candidate))
    return sorted(ranked, key=lambda item: item[0], reverse=True)
