from __future__ import annotations

import asyncio
import time
import uuid
from collections import Counter, deque
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import TypeVar

from .agent import (
    ResearchAgent,
    fallback_answer,
    heuristic_evidence,
    heuristic_spec,
)
from .config import Budget
from .evidence import EvidenceLedger
from .models import ResearchResult, ResearchStats, SearchResult
from .ranking import rank_candidates
from .readers.base import Reader
from .safety.urls import registrable_domain
from .search.base import SearchProvider
from .storage import SQLiteStore

ProgressCallback = Callable[[float, str], Awaitable[None]]
T = TypeVar("T")
THOROUGH_MIN_SEARCHES = 3
THOROUGH_MIN_USABLE_DOMAINS = 6


async def _no_progress(_progress: float, _message: str) -> None:
    return None


class ResearchController:
    def __init__(
        self,
        *,
        search: SearchProvider,
        reader: Reader,
        agent: ResearchAgent,
        store: SQLiteStore,
    ) -> None:
        self.search = search
        self.reader = reader
        self.agent = agent
        self.store = store

    async def run(
        self,
        query: str,
        *,
        effort: str,
        freshness: str | None,
        budget: Budget,
        progress: ProgressCallback | None = None,
    ) -> ResearchResult:
        research_id = str(uuid.uuid4())
        started = time.monotonic()
        self.store.start_run(research_id, query, effort)
        try:
            return await self._run_started(
                query,
                effort=effort,
                freshness=freshness,
                budget=budget,
                research_id=research_id,
                started=started,
                progress=progress,
            )
        except asyncio.CancelledError:
            self.store.cancel_run(research_id)
            raise

    async def _run_started(
        self,
        query: str,
        *,
        effort: str,
        freshness: str | None,
        budget: Budget,
        research_id: str,
        started: float,
        progress: ProgressCallback | None,
    ) -> ResearchResult:
        callback = progress or _no_progress
        browsing_budget = _ActiveTimeBudget(budget.max_seconds)
        stats = ResearchStats()
        warnings: list[str] = []

        await callback(0.02, "Compiling the research requirements")
        try:
            spec = await self.agent.compile_spec(query, freshness)
        except Exception as exc:
            spec = heuristic_spec(query, freshness)
            warnings.append(f"research_spec_fallback: {type(exc).__name__}: {exc}")
        self.store.event(research_id, "research_spec", _spec_payload(spec))
        ledger = EvidenceLedger(spec)

        try:
            initial_queries = await self.agent.plan_queries(spec)
        except Exception as exc:
            initial_queries = [query]
            warnings.append(f"query_planning_fallback: {type(exc).__name__}: {exc}")
        if not initial_queries:
            initial_queries = [query]
        pending_queries = deque(initial_queries)
        seen_queries: set[str] = set()
        candidates: list[SearchResult] = []
        candidate_urls: set[str] = set()
        fetched_urls: set[str] = set()
        domain_counts: Counter[str] = Counter()
        low_gain_streak = 0
        sufficient_streak = 0
        stop_reason = "search_exhausted"

        try:
            while True:
                if browsing_budget.exhausted:
                    stop_reason = "time_budget_exhausted"
                    break
                if stats.pages_fetched >= budget.max_pages:
                    stop_reason = "page_budget_exhausted"
                    break

                coverage = ledger.coverage()

                if not candidates:
                    if pending_queries and stats.search_queries < budget.max_searches:
                        search_query = pending_queries.popleft()
                        query_key = search_query.casefold().strip()
                        if not query_key or query_key in seen_queries:
                            continue
                        seen_queries.add(query_key)
                        stats.search_queries += 1
                        await callback(
                            _progress(stats, coverage.score, budget),
                            f"Searching: {search_query}",
                        )
                        try:
                            results = await browsing_budget.run(
                                lambda search_query=search_query: self.search.search(
                                    search_query,
                                    language=spec.locale,
                                    time_range=_searxng_time_range(freshness),
                                    limit=12,
                                )
                            )
                        except Exception as exc:
                            warnings.append(f"search_failed: {type(exc).__name__}: {exc}")
                            self.store.event(
                                research_id,
                                "search_failed",
                                {"query": search_query, "error": str(exc)},
                            )
                            continue
                        self.store.event(
                            research_id,
                            "search_results",
                            {"query": search_query, "count": len(results)},
                        )
                        for result in results:
                            if result.url not in candidate_urls and result.url not in fetched_urls:
                                candidates.append(result)
                                candidate_urls.add(result.url)
                        continue

                    needs_depth = not _research_depth_satisfied(effort, stats, ledger)
                    if (
                        not coverage.sufficient or needs_depth
                    ) and stats.search_queries < budget.max_searches:
                        followups = await self._followup_queries(spec, ledger, warnings)
                        novel = [
                            item
                            for item in followups
                            if item.casefold().strip() not in seen_queries
                        ]
                        if not novel and needs_depth:
                            novel = [
                                item
                                for item in _depth_queries(spec.original_query)
                                if item.casefold().strip() not in seen_queries
                            ]
                        if novel:
                            pending_queries.extend(novel)
                            continue
                    stop_reason = (
                        "requirements_satisfied_and_frontier_exhausted"
                        if coverage.sufficient
                        else "search_frontier_exhausted_with_gaps"
                    )
                    break

                ranked = rank_candidates(
                    candidates,
                    spec,
                    coverage.unresolved_gaps,
                    domain_counts,
                )
                selected: SearchResult | None = None
                selected_score = 0.0
                for score, candidate in ranked:
                    domain = registrable_domain(candidate.url)
                    if domain_counts[domain] < budget.max_pages_per_domain:
                        selected = candidate
                        selected_score = score
                        break
                if selected is None:
                    candidates.clear()
                    continue
                candidates.remove(selected)
                candidate_urls.discard(selected.url)

                depth_satisfied = _research_depth_satisfied(effort, stats, ledger)
                if coverage.sufficient and depth_satisfied and selected_score < budget.min_gain:
                    stop_reason = "requirements_satisfied_and_expected_gain_low"
                    break

                domain = registrable_domain(selected.url)
                await callback(
                    _progress(stats, coverage.score, budget),
                    f"Reading {domain}: {selected.title[:90]}",
                )
                try:
                    document = await browsing_budget.run(
                        lambda selected=selected: self.reader.read(selected.url)
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    stats.fetch_failures += 1
                    fetched_urls.add(selected.url)
                    warnings.append(f"fetch_failed {selected.url}: {type(exc).__name__}: {exc}")
                    self.store.event(
                        research_id,
                        "fetch_failed",
                        {"url": selected.url, "error": str(exc)},
                    )
                    continue

                fetched_urls.add(selected.url)
                domain_counts[domain] += 1
                stats.pages_fetched += 1
                if "cache_hit" in document.warnings:
                    stats.cache_hits += 1
                try:
                    batch = await self.agent.analyze_document(spec, document)
                except Exception as exc:
                    batch = heuristic_evidence(spec, document)
                    warnings.append(
                        f"evidence_extraction_fallback {selected.url}: {type(exc).__name__}: {exc}"
                    )
                source, claims_added = ledger.add_document(document, batch)
                low_gain_streak = low_gain_streak + 1 if claims_added == 0 else 0
                self.store.event(
                    research_id,
                    "document_analyzed",
                    {
                        "source": asdict(source),
                        "claims_added": claims_added,
                        "candidate_score": selected_score,
                    },
                )

                coverage = ledger.coverage()
                if coverage.sufficient:
                    sufficient_streak += 1
                else:
                    sufficient_streak = 0
                await callback(
                    _progress(stats, coverage.score, budget),
                    f"Coverage {coverage.score:.0%}; "
                    f"{len(coverage.unresolved_gaps)} required gap(s) remain",
                )

                at_checkpoint = stats.pages_fetched % budget.checkpoint_every_pages == 0
                if at_checkpoint:
                    if coverage.sufficient:
                        if not _research_depth_satisfied(effort, stats, ledger):
                            sufficient_streak = 0
                            if effort == "thorough" and pending_queries:
                                candidates.clear()
                                candidate_urls.clear()
                            continue
                        if sufficient_streak >= 2 or low_gain_streak >= 2:
                            stop_reason = "requirements_satisfied_and_saturated"
                            break
                    else:
                        sufficient_streak = 0
                        followups = await self._followup_queries(spec, ledger, warnings)
                        pending_queries.extend(
                            item
                            for item in followups
                            if item.casefold().strip() not in seen_queries
                        )
        except asyncio.CancelledError:
            raise

        coverage = ledger.coverage()
        await callback(0.94, "Writing and validating the cited answer")
        try:
            answer = await self.agent.synthesize(spec, ledger)
        except Exception as exc:
            answer = fallback_answer(spec, ledger)
            warnings.append(f"answer_synthesis_fallback: {type(exc).__name__}: {exc}")

        stats.independent_domains = len({item.domain for item in ledger.evidence_sources()})
        stats.elapsed_ms = int((time.monotonic() - started) * 1000)
        stats.browsing_elapsed_ms = int(browsing_budget.elapsed * 1000)
        result = ResearchResult(
            research_id=research_id,
            answer_markdown=answer,
            sources=ledger.evidence_sources(),
            coverage=coverage,
            stop_reason=stop_reason,
            stats=stats,
            warnings=warnings,
        )
        self.store.finish_run(result)
        await callback(1.0, f"Research complete: {stop_reason}")
        return result

    async def _followup_queries(
        self,
        spec,
        ledger: EvidenceLedger,
        warnings: list[str],
    ) -> list[str]:
        try:
            assessment = await self.agent.assess(spec, ledger)
            queries = assessment.get("followup_queries")
            if isinstance(queries, list):
                return [" ".join(str(item).split()) for item in queries if str(item).strip()]
        except Exception as exc:
            warnings.append(f"gap_assessment_failed: {type(exc).__name__}: {exc}")
        gap_questions = [
            item.question
            for item in spec.requirements
            if item.id in ledger.coverage().unresolved_gaps
        ]
        return gap_questions[:3]


def _progress(stats: ResearchStats, coverage_score: float, budget: Budget) -> float:
    budget_fraction = stats.pages_fetched / max(1, budget.max_pages)
    return min(0.9, 0.08 + 0.42 * budget_fraction + 0.4 * coverage_score)


def _research_depth_satisfied(effort: str, stats: ResearchStats, ledger: EvidenceLedger) -> bool:
    if effort != "thorough":
        return True
    usable_domains = {source.domain for source in ledger.evidence_sources()}
    return (
        stats.search_queries >= THOROUGH_MIN_SEARCHES
        and len(usable_domains) >= THOROUGH_MIN_USABLE_DOMAINS
    )


def _depth_queries(query: str) -> list[str]:
    return [
        f"{query} official release notes documentation",
        f"{query} independent analysis review",
        f"{query} technical report primary source",
        f"{query} expert coverage",
    ]


class _ActiveTimeBudget:
    """Counts only time spent awaiting search and document retrieval."""

    def __init__(self, seconds: float) -> None:
        self.limit = max(0.0, seconds)
        self.remaining = self.limit

    @property
    def elapsed(self) -> float:
        return self.limit - self.remaining

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0

    async def run(self, factory: Callable[[], Awaitable[T]]) -> T:
        if self.exhausted:
            raise TimeoutError("active browsing time budget exhausted")
        started = time.monotonic()
        try:
            return await asyncio.wait_for(factory(), timeout=self.remaining)
        finally:
            self.remaining = max(0.0, self.remaining - (time.monotonic() - started))


def _searxng_time_range(freshness: str | None) -> str | None:
    if not freshness:
        return None
    lower = freshness.lower()
    if any(token in lower for token in ("today", "24 hour", "day")):
        return "day"
    if any(token in lower for token in ("month", "30 day", "recent")):
        return "month"
    if any(token in lower for token in ("year", "12 month")):
        return "year"
    return None


def _spec_payload(spec) -> dict:
    return {
        "original_query": spec.original_query,
        "task_type": spec.task_type,
        "subjects": spec.subjects,
        "requirements": [asdict(item) for item in spec.requirements],
        "freshness": spec.freshness,
        "locale": spec.locale,
    }
