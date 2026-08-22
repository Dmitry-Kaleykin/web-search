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
        primary_hint = 0.0
        if any(item.primary_required for item in uncovered) and any(
            token in domain for token in ("docs.", "support.", "developer.")
        ):
            primary_hint = 0.12
        score = 0.58 * relevance + 0.18 * result_rank + 0.16 * diversity + engine_bonus
        score += primary_hint
        ranked.append((score, candidate))
    return sorted(ranked, key=lambda item: item[0], reverse=True)
