from __future__ import annotations

import asyncio
import time
import uuid
from collections import Counter, deque
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from functools import partial
from typing import TypeVar
from urllib.parse import unquote, urlsplit

from .agent import (
    ResearchAgent,
    fallback_answer,
    heuristic_evidence,
    heuristic_spec,
)
from .config import Budget
from .dates import normalize_published_at
from .evidence import EvidenceLedger
from .models import Document, PlannedQuery, ResearchResult, ResearchStats, SearchLane, SearchResult
from .ranking import rank_candidates
from .readers.base import Reader
from .reranking import CandidateReranker, RerankingError
from .safety.urls import canonicalize_url, registrable_domain
from .search.base import SearchProvider
from .storage import SQLiteStore
from .text import lexical_similarity

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
        reranker: CandidateReranker | None = None,
        prefetch_pages: int = 1,
    ) -> None:
        self.search = search
        self.reader = reader
        self.agent = agent
        self.store = store
        self.reranker = reranker
        self.prefetch_pages = max(1, prefetch_pages)

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
        run_deadline = _RunDeadline(budget.max_wall_seconds)
        synthesis_reserve = budget.synthesis_reserve_seconds
        stats = ResearchStats()
        warnings: list[str] = []
        deadline_reached = False

        await callback(0.02, "Compiling the research requirements")
        try:
            spec = await run_deadline.run(
                lambda: self.agent.compile_spec(query, freshness),
                reserve=synthesis_reserve,
            )
        except _RunDeadlineExceeded:
            spec = heuristic_spec(query, freshness)
            deadline_reached = True
            warnings.append("research_spec_fallback: internal research deadline reached")
        except Exception as exc:
            spec = heuristic_spec(query, freshness)
            warnings.append(f"research_spec_fallback: {type(exc).__name__}: {exc}")
        self.store.event(research_id, "research_spec", _spec_payload(spec))
        ledger = EvidenceLedger(spec)

        try:
            initial_queries = await run_deadline.run(
                lambda: self.agent.plan_queries(spec),
                reserve=synthesis_reserve,
            )
        except _RunDeadlineExceeded:
            initial_queries = [PlannedQuery(query=query)]
            deadline_reached = True
            warnings.append("query_planning_fallback: internal research deadline reached")
        except Exception as exc:
            initial_queries = [PlannedQuery(query=query)]
            warnings.append(f"query_planning_fallback: {type(exc).__name__}: {exc}")
        if not initial_queries:
            initial_queries = [PlannedQuery(query=query)]
        pending_queries = deque(initial_queries)
        seen_queries: set[str] = set()
        candidates: list[SearchResult] = []
        deferred_candidates: list[SearchResult] = []
        discovered_urls: set[str] = set()
        fetched_urls: set[str] = set()
        domain_counts: Counter[str] = Counter()
        attempts_in_search_batch = 0
        low_gain_streak = 0
        rerank_scores: dict[str, float] = {}
        prefetch_tasks: dict[str, asyncio.Task[Document]] = {}
        sufficient_streak = 0
        stop_reason = "search_exhausted"

        try:
            while True:
                if run_deadline.exhausted(reserve=synthesis_reserve):
                    deadline_reached = True
                    stop_reason = "internal_deadline_reached"
                    break
                if browsing_budget.exhausted:
                    stop_reason = "time_budget_exhausted"
                    break
                if stats.pages_fetched >= budget.max_pages:
                    stop_reason = "page_budget_exhausted"
                    break

                coverage = ledger.coverage()

                if not candidates:
                    if pending_queries and stats.search_queries < budget.max_searches:
                        planned_query = pending_queries.popleft()
                        search_query = planned_query.query
                        query_key = f"{planned_query.lane}:{search_query.casefold().strip()}"
                        if not query_key or query_key in seen_queries:
                            continue
                        seen_queries.add(query_key)
                        stats.search_queries += 1
                        await callback(
                            _progress(stats, coverage.score, budget),
                            f"Searching{_lane_label(planned_query.lane)}: {search_query}",
                        )
                        try:
                            results, lane_fallback = await run_deadline.run(
                                partial(
                                    _search_with_lane,
                                    self.search,
                                    browsing_budget,
                                    search_query,
                                    planned_query.lane,
                                    spec.locale,
                                    _searxng_time_range(freshness),
                                ),
                                reserve=synthesis_reserve,
                            )
                        except _RunDeadlineExceeded:
                            raise
                        except Exception as exc:
                            warnings.append(f"search_failed: {type(exc).__name__}: {exc}")
                            self.store.event(
                                research_id,
                                "search_failed",
                                {"query": search_query, "error": str(exc)},
                            )
                            continue
                        if lane_fallback:
                            warnings.append(
                                f"source_lane_empty:{planned_query.lane}:used_web_fallback"
                            )
                            await callback(
                                _progress(stats, coverage.score, budget),
                                f"No {planned_query.lane} results; retried the general web lane",
                            )
                        self.store.event(
                            research_id,
                            "search_results",
                            {
                                "query": search_query,
                                "lane": planned_query.lane,
                                "count": len(results),
                            },
                        )
                        attempts_in_search_batch = 0
                        novel_results: list[SearchResult] = []
                        for result in results:
                            if result.url not in discovered_urls and result.url not in fetched_urls:
                                candidates.append(result)
                                novel_results.append(result)
                                discovered_urls.add(result.url)
                        if self.reranker is not None and novel_results:
                            await callback(
                                _progress(stats, coverage.score, budget),
                                f"Reranking {len(novel_results)} search candidates with "
                                f"{self.reranker.model}",
                            )
                            rerank_query = _rerank_query(spec, coverage.unresolved_gaps)
                            try:
                                rerank_scores.update(
                                    await run_deadline.run(
                                        partial(
                                            self.reranker.rerank,
                                            rerank_query,
                                            novel_results,
                                        ),
                                        reserve=synthesis_reserve,
                                    )
                                )
                            except _RunDeadlineExceeded:
                                raise
                            except RerankingError as exc:
                                warnings.append(f"reranker_fallback: {exc}")
                                await callback(
                                    _progress(stats, coverage.score, budget),
                                    "Semantic reranker unavailable; using deterministic ranking",
                                )
                        continue

                    needs_depth = not _research_depth_satisfied(effort, stats, ledger)
                    if (
                        not coverage.sufficient or needs_depth
                    ) and stats.search_queries < budget.max_searches:
                        followups = await self._followup_queries(
                            spec,
                            ledger,
                            warnings,
                            deadline=run_deadline,
                            reserve=synthesis_reserve,
                        )
                        novel = [
                            item
                            for item in followups
                            if f"{item.lane}:{item.query.casefold().strip()}" not in seen_queries
                        ]
                        if not novel and needs_depth:
                            novel = [
                                item
                                for item in _depth_queries(spec.original_query)
                                if f"{item.lane}:{item.query.casefold().strip()}"
                                not in seen_queries
                            ]
                        if novel:
                            pending_queries.extend(novel)
                            continue
                    if (not coverage.sufficient or needs_depth) and deferred_candidates:
                        candidates.extend(deferred_candidates)
                        deferred_candidates.clear()
                        attempts_in_search_batch = 0
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
                    rerank_scores,
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
                eligible_ranked = [
                    candidate
                    for _, candidate in ranked
                    if domain_counts[registrable_domain(candidate.url)]
                    < budget.max_pages_per_domain
                ]
                for prefetch_candidate in eligible_ranked[: self.prefetch_pages]:
                    if prefetch_candidate.url in prefetch_tasks:
                        continue
                    prefetch_tasks[prefetch_candidate.url] = asyncio.create_task(
                        run_deadline.run(
                            lambda candidate=prefetch_candidate: browsing_budget.run(
                                lambda: self.reader.read(candidate.url)
                            ),
                            reserve=synthesis_reserve,
                        )
                    )
                    stats.prefetch_started += 1
                candidates.remove(selected)
                attempts_in_search_batch += 1

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
                    prefetched = prefetch_tasks.pop(selected.url, None)
                    if prefetched is not None:
                        document = await prefetched
                    else:
                        document = await run_deadline.run(
                            lambda selected=selected: browsing_budget.run(
                                lambda: self.reader.read(selected.url)
                            ),
                            reserve=synthesis_reserve,
                        )
                except _RunDeadlineExceeded:
                    raise
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
                    if _search_batch_exhausted(attempts_in_search_batch, stats, budget):
                        deferred_candidates.extend(candidates)
                        candidates.clear()
                        attempts_in_search_batch = 0
                    continue

                fetched_urls.add(selected.url)
                if document.published_at is None and selected.published_at:
                    document.published_at = normalize_published_at(selected.published_at)
                    if document.published_at:
                        document.published_at_source = "search_result"
                        document.warnings.append("published_at_from_search_result")
                domain_counts[domain] += 1
                stats.pages_fetched += 1
                if "cache_hit" in document.warnings:
                    stats.cache_hits += 1
                usage_before = self.agent.evidence_model_usage()
                if usage_before["disabled"]:
                    analysis_model = "Pi active model (dedicated evidence model disabled)"
                elif usage_before["model"] == "pi-active":
                    analysis_model = "Pi active model"
                else:
                    analysis_model = str(usage_before["model"])
                await callback(
                    _progress(stats, coverage.score, budget),
                    f"Analyzing extracted evidence with {analysis_model}",
                )
                analysis_hit_deadline = False
                try:
                    batch = await run_deadline.run(
                        lambda document=document: self.agent.analyze_document(spec, document),
                        reserve=synthesis_reserve,
                    )
                except _RunDeadlineExceeded:
                    batch = heuristic_evidence(spec, document)
                    analysis_hit_deadline = True
                    deadline_reached = True
                    warnings.append(
                        f"evidence_extraction_fallback {selected.url}: "
                        "internal research deadline reached"
                    )
                except Exception as exc:
                    batch = heuristic_evidence(spec, document)
                    warnings.append(
                        f"evidence_extraction_fallback {selected.url}: {type(exc).__name__}: {exc}"
                    )
                usage_after = self.agent.evidence_model_usage()
                if int(usage_after["fallbacks"]) > int(usage_before["fallbacks"]):
                    fallback_message = (
                        f"Evidence model {usage_after['model']} unavailable; used Pi active model"
                    )
                    if usage_after["disabled"]:
                        fallback_message += " and disabled the helper for this search"
                    await callback(
                        _progress(stats, coverage.score, budget),
                        fallback_message,
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

                previous_conflicts = set(coverage.conflicts)
                coverage = ledger.coverage()
                if not coverage.sufficient:
                    linked_candidates = _linked_candidates(
                        document,
                        spec,
                        coverage.unresolved_gaps,
                        discovered_urls | fetched_urls,
                    )
                    for linked in linked_candidates:
                        candidates.append(linked)
                        discovered_urls.add(linked.url)
                    stats.followed_links_discovered += len(linked_candidates)
                if coverage.sufficient:
                    sufficient_streak += 1
                else:
                    sufficient_streak = 0
                await callback(
                    _progress(stats, coverage.score, budget),
                    _evidence_checkpoint(
                        ledger,
                        source.domain,
                        claims_added,
                        coverage.score,
                        coverage.unresolved_gaps,
                        previous_conflicts,
                        coverage.conflicts,
                    ),
                )

                if analysis_hit_deadline:
                    stop_reason = "internal_deadline_reached"
                    break

                if _search_batch_exhausted(attempts_in_search_batch, stats, budget):
                    deferred_candidates.extend(candidates)
                    candidates.clear()
                    attempts_in_search_batch = 0

                at_checkpoint = stats.pages_fetched % budget.checkpoint_every_pages == 0
                if at_checkpoint:
                    if coverage.sufficient:
                        if not _research_depth_satisfied(effort, stats, ledger):
                            sufficient_streak = 0
                            if effort == "thorough" and pending_queries:
                                deferred_candidates.extend(candidates)
                                candidates.clear()
                                attempts_in_search_batch = 0
                            continue
                        if sufficient_streak >= 2 or low_gain_streak >= 2:
                            stop_reason = "requirements_satisfied_and_saturated"
                            break
                    else:
                        sufficient_streak = 0
                        # Deterministic coverage already tells us whether more evidence is needed.
                        # Defer the comparatively expensive model assessment until this result
                        # batch is exhausted and a new query actually has to be planned.
                        continue
        except _RunDeadlineExceeded:
            deadline_reached = True
            stop_reason = "internal_deadline_reached"
        except asyncio.CancelledError:
            await _cancel_prefetch(prefetch_tasks)
            raise

        stats.prefetch_unused = len(prefetch_tasks)
        await _cancel_prefetch(prefetch_tasks)
        coverage = ledger.coverage()
        await callback(0.94, "Writing and validating the cited answer")
        try:
            answer = await run_deadline.run(
                lambda: self.agent.synthesize(spec, ledger),
                reserve=0.0,
            )
        except _RunDeadlineExceeded:
            answer = fallback_answer(spec, ledger)
            deadline_reached = True
            stop_reason = "internal_deadline_reached"
            warnings.append("answer_synthesis_fallback: internal run deadline reached")
        except Exception as exc:
            answer = fallback_answer(spec, ledger)
            warnings.append(f"answer_synthesis_fallback: {type(exc).__name__}: {exc}")

        if deadline_reached:
            deadline_warning = "internal_deadline_reached: returning best available evidence"
            if deadline_warning not in warnings:
                warnings.append(deadline_warning)
            self.store.event(
                research_id,
                "internal_deadline_reached",
                {"max_wall_seconds": budget.max_wall_seconds},
            )

        stats.distinct_domains = len({item.domain for item in ledger.evidence_sources()})
        stats.elapsed_ms = int((time.monotonic() - started) * 1000)
        stats.browsing_elapsed_ms = int(browsing_budget.elapsed * 1000)
        model_usage = self.agent.evidence_model_usage()
        stats.evidence_model = str(model_usage["model"])
        stats.evidence_model_attempts = int(model_usage["attempts"])
        stats.evidence_model_successes = int(model_usage["successes"])
        stats.evidence_model_failures = int(model_usage["failures"])
        stats.evidence_model_fallbacks = int(model_usage["fallbacks"])
        stats.evidence_model_disabled = bool(model_usage["disabled"])
        self.store.event(research_id, "evidence_model_usage", model_usage)
        if self.reranker is not None:
            reranker_usage = self.reranker.usage()
            stats.reranker_model = self.reranker.model
            stats.reranker_requests = reranker_usage.requests
            stats.reranker_candidates = reranker_usage.candidates
            stats.reranker_failures = reranker_usage.failures
            stats.reranker_disabled = reranker_usage.disabled
            self.store.event(research_id, "reranker_usage", asdict(reranker_usage))
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
        *,
        deadline: _RunDeadline,
        reserve: float,
    ) -> list[PlannedQuery]:
        try:
            assessment = await deadline.run(
                lambda: self.agent.assess(spec, ledger),
                reserve=reserve,
            )
            queries = assessment.get("followup_queries")
            if isinstance(queries, list):
                return _coerce_planned_queries(queries)
        except _RunDeadlineExceeded:
            raise
        except Exception as exc:
            warnings.append(f"gap_assessment_failed: {type(exc).__name__}: {exc}")
        gap_questions = [
            PlannedQuery(query=item.question, lane=item.search_lane)
            for item in spec.requirements
            if item.id in ledger.coverage().unresolved_gaps
        ]
        return gap_questions[:3]


def _progress(stats: ResearchStats, coverage_score: float, budget: Budget) -> float:
    budget_fraction = stats.pages_fetched / max(1, budget.max_pages)
    return min(0.9, 0.08 + 0.42 * budget_fraction + 0.4 * coverage_score)


def _evidence_checkpoint(
    ledger: EvidenceLedger,
    domain: str,
    claims_added: int,
    coverage_score: float,
    unresolved_gaps: list[str],
    previous_conflicts: set[str],
    conflicts: list[str],
) -> str:
    new_conflicts = [item for item in conflicts if item not in previous_conflicts]
    if new_conflicts:
        summary = f"Conflict detected: {_short_progress_text(new_conflicts[0], 150)}"
    elif claims_added:
        latest = ledger.claims[-claims_added:]
        statement = latest[0].statement if latest else "New supporting evidence"
        extra = f" (+{claims_added - 1} more)" if claims_added > 1 else ""
        summary = f"Found: {_short_progress_text(statement, 145)}{extra} [{domain}]"
    else:
        summary = f"No usable new evidence from {domain}"
    return (
        f"{summary}\nCoverage {coverage_score:.0%}; "
        f"{len(unresolved_gaps)} required gap(s) remain"
    )


def _short_progress_text(value: str, limit: int) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return f"{text[: max(1, limit - 1)].rstrip()}…"


def _rerank_query(spec, unresolved_requirement_ids: list[str]) -> str:
    unresolved = set(unresolved_requirement_ids)
    questions = [
        item.question for item in spec.requirements if not unresolved or item.id in unresolved
    ]
    return "\n".join([spec.original_query, *questions])[:8_000]


def _research_depth_satisfied(effort: str, stats: ResearchStats, ledger: EvidenceLedger) -> bool:
    if effort != "thorough":
        return True
    usable_domains = {source.domain for source in ledger.evidence_sources()}
    return (
        stats.search_queries >= THOROUGH_MIN_SEARCHES
        and len(usable_domains) >= THOROUGH_MIN_USABLE_DOMAINS
    )


def _depth_queries(query: str) -> list[PlannedQuery]:
    return [
        PlannedQuery(f"{query} publisher release notes documentation", SearchLane.DOCUMENTATION),
        PlannedQuery(f"{query} independent analysis review"),
        PlannedQuery(f"{query} technical report documentation", SearchLane.ACADEMIC),
        PlannedQuery(f"{query} expert coverage"),
    ]


def _coerce_planned_queries(values: list[object]) -> list[PlannedQuery]:
    queries: list[PlannedQuery] = []
    for value in values:
        if isinstance(value, dict):
            query = " ".join(str(value.get("query") or "").split())
            lane_value = str(value.get("lane") or "web")
        else:
            query = " ".join(str(value).split())
            lane_value = "web"
        if not query:
            continue
        try:
            lane = SearchLane(lane_value)
        except ValueError:
            lane = SearchLane.WEB
        queries.append(PlannedQuery(query, lane))
    return queries


def _lane_query(query: str, lane: SearchLane) -> str:
    if lane == SearchLane.DOCUMENTATION and "documentation" not in query.casefold():
        return f"{query} documentation"
    return query


def _lane_categories(lane: SearchLane) -> str | None:
    return {
        SearchLane.ACADEMIC: "science",
        SearchLane.COMMUNITY: "social media",
    }.get(lane)


def _lane_label(lane: SearchLane) -> str:
    return "" if lane == SearchLane.WEB else f" [{lane}]"


async def _search_with_lane(
    search: SearchProvider,
    browsing_budget: _ActiveTimeBudget,
    query: str,
    lane: SearchLane,
    language: str | None,
    time_range: str | None,
) -> tuple[list[SearchResult], bool]:
    results = await browsing_budget.run(
        lambda: search.search(
            _lane_query(query, lane),
            language=language,
            time_range=time_range,
            categories=_lane_categories(lane),
            limit=12,
        )
    )
    if results or lane == SearchLane.WEB:
        return results, False
    fallback = await browsing_budget.run(
        lambda: search.search(
            query,
            language=language,
            time_range=time_range,
            categories=None,
            limit=12,
        )
    )
    return fallback, True


def _linked_candidates(
    document: Document,
    spec,
    unresolved_requirement_ids: list[str],
    seen_urls: set[str],
    *,
    limit: int = 6,
) -> list[SearchResult]:
    unresolved = set(unresolved_requirement_ids)
    targets = [
        item.question for item in spec.requirements if not unresolved or item.id in unresolved
    ]
    parent_domain = registrable_domain(document.final_url)
    candidates: list[tuple[float, SearchResult]] = []
    hints = {
        "changelog",
        "comparison",
        "docs",
        "documentation",
        "pricing",
        "release",
        "review",
        "spec",
        "specification",
        "support",
    }
    for rank, raw_url in enumerate(document.links, start=1):
        try:
            url = canonicalize_url(raw_url)
        except ValueError:
            continue
        if url in seen_urls or registrable_domain(url) != parent_domain:
            continue
        parsed = urlsplit(url)
        path_text = unquote(parsed.path.replace("-", " ").replace("_", " "))
        path_tokens = {token.casefold() for token in path_text.replace("/", " ").split()}
        relevance = max((lexical_similarity(path_text, target) for target in targets), default=0.0)
        if relevance < 0.03 and not path_tokens.intersection(hints):
            continue
        title = path_text.strip(" /") or parsed.hostname or url
        candidates.append(
            (
                relevance,
                SearchResult(
                    url=url,
                    title=title,
                    snippet=f"Linked from {document.title}",
                    rank=rank,
                ),
            )
        )
    return [item for _, item in sorted(candidates, key=lambda pair: pair[0], reverse=True)[:limit]]


def _search_batch_exhausted(
    attempts: int,
    stats: ResearchStats,
    budget: Budget,
) -> bool:
    return (
        attempts >= max(1, budget.max_attempts_per_search_batch)
        and stats.search_queries < budget.max_searches
    )


class _RunDeadlineExceeded(TimeoutError):
    pass


class _RunDeadline:
    """Bounds the full run while reserving time for final synthesis."""

    def __init__(self, seconds: float | None) -> None:
        self.deadline = time.monotonic() + max(0.0, seconds) if seconds is not None else None

    @property
    def remaining(self) -> float:
        if self.deadline is None:
            return float("inf")
        return max(0.0, self.deadline - time.monotonic())

    def exhausted(self, *, reserve: float) -> bool:
        return self.deadline is not None and self.remaining <= max(0.0, reserve)

    async def run(
        self,
        factory: Callable[[], Awaitable[T]],
        *,
        reserve: float,
    ) -> T:
        if self.deadline is None:
            return await factory()
        available = self.remaining - max(0.0, reserve)
        if available <= 0:
            raise _RunDeadlineExceeded("internal run deadline reached")
        try:
            return await asyncio.wait_for(factory(), timeout=available)
        except TimeoutError as exc:
            if self.exhausted(reserve=reserve):
                raise _RunDeadlineExceeded("internal run deadline reached") from exc
            raise


class _ActiveTimeBudget:
    """Counts only time spent awaiting search and document retrieval."""

    def __init__(self, seconds: float) -> None:
        self.limit = max(0.0, seconds)
        self.remaining = self.limit
        self._active = 0
        self._active_started = 0.0
        self._lock = asyncio.Lock()

    @property
    def elapsed(self) -> float:
        return self.limit - self.remaining

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0

    async def run(self, factory: Callable[[], Awaitable[T]]) -> T:
        async with self._lock:
            if self.exhausted:
                raise TimeoutError("active browsing time budget exhausted")
            if self._active == 0:
                self._active_started = time.monotonic()
            self._active += 1
            available = self.remaining
        try:
            return await asyncio.wait_for(factory(), timeout=available)
        finally:
            async with self._lock:
                self._active -= 1
                if self._active == 0:
                    self.remaining = max(
                        0.0, self.remaining - (time.monotonic() - self._active_started)
                    )


async def _cancel_prefetch(tasks: dict[str, asyncio.Task[Document]]) -> None:
    pending = list(tasks.values())
    tasks.clear()
    for task in pending:
        if not task.done():
            task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


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
