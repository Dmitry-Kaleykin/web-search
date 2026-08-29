from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from .models import ResearchSpec, SearchResult
from .safety.urls import registrable_domain
from .text import lexical_similarity


@dataclass(frozen=True, slots=True)
class RelevanceGateResult:
    accepted: list[SearchResult]
    rejected: list[tuple[SearchResult, float]]
    mode: str
    threshold: float
    probing: bool = False


def gate_candidates(
    candidates: list[SearchResult],
    *,
    search_query: str,
    spec: ResearchSpec,
    uncovered_requirement_ids: list[str],
    semantic_scores: dict[str, float],
    semantic_min_score: float,
    semantic_relative_ratio: float,
    lexical_min_score: float,
    rejected_batch_streak: int,
) -> RelevanceGateResult:
    """Separate page eligibility from ranking bonuses such as SERP position and diversity."""
    if not candidates:
        return RelevanceGateResult([], [], "none", 0.0)

    relaxation = 0.5 ** min(max(0, rejected_batch_streak), 2)
    has_complete_semantic_scores = all(item.url in semantic_scores for item in candidates)
    if has_complete_semantic_scores:
        scored = [(item, _bounded_score(semantic_scores[item.url])) for item in candidates]
        best_score = max(score for _, score in scored)
        threshold = max(
            _bounded_score(semantic_min_score) * relaxation,
            best_score * _bounded_score(semantic_relative_ratio),
        )
        mode = "semantic"
    else:
        targets = [search_query]
        unresolved = set(uncovered_requirement_ids)
        targets.extend(
            item.question for item in spec.requirements if not unresolved or item.id in unresolved
        )
        scored = [
            (
                item,
                max(
                    lexical_similarity(f"{item.title} {item.snippet}", target) for target in targets
                ),
            )
            for item in candidates
        ]
        threshold = max(0.0, lexical_min_score) * relaxation
        mode = "lexical"

    accepted = [item for item, score in scored if score >= threshold]
    probing = False
    if not accepted and rejected_batch_streak >= 3:
        best_candidate, best_score = max(scored, key=lambda item: item[1])
        if best_score > 0:
            accepted = [best_candidate]
            probing = True
    accepted_urls = {item.url for item in accepted}
    rejected = [(item, score) for item, score in scored if item.url not in accepted_urls]
    return RelevanceGateResult(accepted, rejected, mode, threshold, probing)


def _bounded_score(value: float) -> float:
    return max(0.0, min(1.0, value))


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
