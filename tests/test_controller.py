from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from web_research.agent import ResearchAgent
from web_research.config import Budget
from web_research.controller import (
    ResearchController,
    _ActiveTimeBudget,
    _linked_candidates,
    _search_with_lane,
)
from web_research.models import (
    Document,
    Requirement,
    ResearchSpec,
    SearchLane,
    SearchResult,
    TaskType,
)
from web_research.storage import SQLiteStore


class FakeModel:
    async def close(self) -> None:
        return None

    async def complete_json(self, *, schema_name, **_kwargs):
        if schema_name == "research_spec":
            return {
                "task_type": "fact",
                "subjects": ["Example"],
                "requirements": [
                    {
                        "id": "R1",
                        "question": "What is the verified value?",
                        "importance": "required",
                        "min_sources": 2,
                    }
                ],
                "answer_format": "Short answer",
                "locale": None,
            }
        if schema_name == "search_queries":
            return {"queries": ["verified example value"]}
        if schema_name == "source_evidence":
            return {
                "source_class": "independent",
                "claims": [
                    {
                        "requirement_id": "R1",
                        "statement": "The verified value is 42.",
                        "excerpt": "The verified value is 42.",
                        "confidence": 0.9,
                        "stance": "supports",
                    }
                ],
            }
        if schema_name == "sufficiency_assessment":
            return {
                "should_continue": True,
                "rationale": "Need another independent source",
                "missing_requirement_ids": ["R1"],
                "followup_queries": [],
            }
        if schema_name == "research_answer":
            return {"answer_markdown": "The verified value is 42. [S1] [S2]"}
        raise AssertionError(schema_name)


class FakeSearch:
    async def close(self) -> None:
        return None

    async def search(self, *_args, **_kwargs):
        return [
            SearchResult(
                url="https://one.example/value",
                title="First verification",
                snippet="verified value 42",
                rank=1,
            ),
            SearchResult(
                url="https://two.example/value",
                title="Second verification",
                snippet="verified value 42",
                rank=2,
            ),
        ]


class FakeReader:
    async def close(self) -> None:
        return None

    async def read(self, url):
        return Document(
            url=url,
            final_url=url,
            title="Verification",
            content="The verified value is 42.",
            method="fake",
        )


class SlowModel(FakeModel):
    def __init__(self, delay: float = 0.04) -> None:
        self.delay = delay

    async def close(self) -> None:
        return None

    async def complete_json(self, **kwargs):
        await asyncio.sleep(self.delay)
        return await super().complete_json(**kwargs)


class SlowSearch(FakeSearch):
    async def search(self, *_args, **_kwargs):
        await asyncio.sleep(0.1)
        return await super().search(*_args, **_kwargs)


class ConcurrentReader(FakeReader):
    def __init__(self) -> None:
        self.active = 0
        self.maximum_active = 0

    async def read(self, url):
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        try:
            await asyncio.sleep(0.03)
            return await super().read(url)
        finally:
            self.active -= 1


class DateCapturingModel(FakeModel):
    def __init__(self) -> None:
        self.calls = []

    async def complete_json(self, **kwargs):
        self.calls.append(kwargs)
        return await super().complete_json(**kwargs)


class AssessmentDateCapturingModel(DateCapturingModel):
    async def complete_json(self, **kwargs):
        if kwargs["schema_name"] == "research_spec":
            self.calls.append(kwargs)
            return {
                "task_type": "fact",
                "subjects": ["Example"],
                "requirements": [
                    {
                        "id": "R1",
                        "question": "What is the verified value?",
                        "importance": "required",
                        "min_sources": 3,
                    }
                ],
                "answer_format": "Short answer",
                "locale": None,
            }
        return await super().complete_json(**kwargs)


class BlockingModel:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def close(self) -> None:
        return None

    async def complete_json(self, **_kwargs):
        self.started.set()
        await asyncio.Event().wait()


class DiverseSearch(FakeSearch):
    def __init__(self) -> None:
        self.calls = 0

    async def search(self, *_args, **_kwargs):
        self.calls += 1
        return [
            SearchResult(
                url=f"https://source{self.calls}{suffix}.example/value",
                title="Verification",
                snippet="verified value 42",
                rank=index,
            )
            for index, suffix in enumerate(("a", "b"), start=1)
        ]


class MultiQueryModel(FakeModel):
    async def complete_json(self, *, schema_name, **kwargs):
        if schema_name == "search_queries":
            return {"queries": ["first angle", "second angle"]}
        return await super().complete_json(schema_name=schema_name, **kwargs)


class FourSourceModel(FakeModel):
    async def complete_json(self, *, schema_name, **kwargs):
        if schema_name == "research_spec":
            return {
                "task_type": "fact",
                "subjects": ["Example"],
                "requirements": [
                    {
                        "id": "R1",
                        "question": "What is the verified value?",
                        "importance": "required",
                        "min_sources": 4,
                    }
                ],
                "answer_format": "Short answer",
                "locale": None,
            }
        return await super().complete_json(schema_name=schema_name, **kwargs)


class EventSearch(FakeSearch):
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.calls = 0

    async def search(self, *_args, **_kwargs):
        self.calls += 1
        self.events.append(f"search:{self.calls}")
        return [
            SearchResult(
                url=f"https://source{self.calls}-{index}.example/value",
                title="Verification",
                snippet="verified value 42",
                rank=index,
            )
            for index in range(1, 5)
        ]


class EventReader(FakeReader):
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def read(self, url):
        self.events.append(f"read:{url}")
        return await super().read(url)


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    async def test_internal_deadline_returns_a_fallback_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "test.sqlite3")
            controller = ResearchController(
                search=FakeSearch(),
                reader=FakeReader(),
                agent=ResearchAgent(BlockingModel()),
                store=store,
            )
            started = time.monotonic()

            result = await controller.run(
                "What is the value?",
                effort="quick",
                freshness=None,
                budget=Budget(
                    20,
                    1,
                    1,
                    1,
                    1,
                    0.1,
                    max_wall_seconds=0.08,
                    synthesis_reserve_seconds=0.03,
                ),
            )

            self.assertLess(time.monotonic() - started, 0.3)
            self.assertEqual(result.stop_reason, "internal_deadline_reached")
            self.assertTrue(
                any("internal_deadline_reached" in warning for warning in result.warnings)
            )
            with sqlite3.connect(Path(directory) / "test.sqlite3") as connection:
                events = connection.execute("SELECT event_type FROM events ORDER BY id").fetchall()
            self.assertIn(("internal_deadline_reached",), events)
            store.close()

    async def test_searches_are_interleaved_after_a_small_candidate_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "test.sqlite3")
            events: list[str] = []
            controller = ResearchController(
                search=EventSearch(events),
                reader=EventReader(events),
                agent=ResearchAgent(MultiQueryModel()),
                store=store,
            )

            result = await controller.run(
                "What is the verified value?",
                effort="auto",
                freshness=None,
                budget=Budget(
                    20,
                    2,
                    3,
                    2,
                    99,
                    0.0,
                    max_attempts_per_search_batch=2,
                ),
            )

            read_positions = [
                index for index, event in enumerate(events) if event.startswith("read:")
            ]
            self.assertEqual(result.stats.search_queries, 2)
            self.assertGreaterEqual(len(read_positions), 3)
            self.assertLess(events.index("search:2"), read_positions[2])
            store.close()

    async def test_deferred_candidates_are_reused_when_no_new_query_is_available(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "test.sqlite3")
            events: list[str] = []
            controller = ResearchController(
                search=EventSearch(events),
                reader=EventReader(events),
                agent=ResearchAgent(FourSourceModel()),
                store=store,
            )

            result = await controller.run(
                "What is the verified value?",
                effort="auto",
                freshness=None,
                budget=Budget(
                    20,
                    2,
                    4,
                    4,
                    99,
                    0.0,
                    max_attempts_per_search_batch=2,
                ),
            )

            self.assertTrue(result.coverage.sufficient)
            self.assertEqual(result.stats.search_queries, 1)
            self.assertEqual(result.stats.pages_fetched, 4)
            store.close()

    async def test_cancellation_during_planning_finalizes_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "test.sqlite3"
            store = SQLiteStore(database)
            model = BlockingModel()
            controller = ResearchController(
                search=FakeSearch(),
                reader=FakeReader(),
                agent=ResearchAgent(model),
                store=store,
            )
            task = asyncio.create_task(
                controller.run(
                    "What is the value?",
                    effort="quick",
                    freshness=None,
                    budget=Budget(20, 1, 1, 1, 1, 0.1),
                )
            )
            await asyncio.wait_for(model.started.wait(), timeout=1)

            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

            with sqlite3.connect(database) as connection:
                row = connection.execute(
                    "SELECT completed_at, result FROM research_runs"
                ).fetchone()
                events = connection.execute("SELECT event_type FROM events").fetchall()
            self.assertIsNotNone(row[0])
            self.assertIsNone(row[1])
            self.assertEqual(events, [("cancelled",)])
            store.close()

    async def test_thorough_research_requires_broader_source_mix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "test.sqlite3")
            search = DiverseSearch()
            controller = ResearchController(
                search=search,
                reader=FakeReader(),
                agent=ResearchAgent(FakeModel()),
                store=store,
            )

            result = await controller.run(
                "What is the verified value?",
                effort="thorough",
                freshness=None,
                budget=Budget(20, 5, 10, 2, 1, 0.1),
            )

            self.assertGreaterEqual(result.stats.search_queries, 3)
            self.assertGreaterEqual(result.stats.distinct_domains, 6)
            self.assertGreaterEqual(result.stats.pages_fetched, 6)
            store.close()

    async def test_every_model_stage_receives_authoritative_current_date(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "test.sqlite3")
            model = AssessmentDateCapturingModel()
            controller = ResearchController(
                search=FakeSearch(),
                reader=FakeReader(),
                agent=ResearchAgent(model, current_date="2031-04-05"),
                store=store,
            )

            await controller.run(
                "What is the latest verified value?",
                effort="quick",
                freshness=None,
                budget=Budget(20, 2, 3, 2, 1, 0.1),
            )

            self.assertEqual(
                [call["schema_name"] for call in model.calls],
                [
                    "research_spec",
                    "search_queries",
                    "source_evidence",
                    "source_evidence",
                    "sufficiency_assessment",
                    "research_answer",
                ],
            )
            self.assertTrue(all("2031-04-05" in call["user"] for call in model.calls), model.calls)
            store.close()

    async def test_controller_reads_until_evidence_rule_is_met(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "test.sqlite3")
            controller = ResearchController(
                search=FakeSearch(),
                reader=FakeReader(),
                agent=ResearchAgent(FakeModel()),
                store=store,
            )
            updates = []

            async def progress(value, message):
                updates.append((value, message))

            result = await controller.run(
                "What is the verified value?",
                effort="auto",
                freshness=None,
                budget=Budget(20, 4, 5, 2, 2, 0.05),
                progress=progress,
            )
            self.assertTrue(result.coverage.sufficient)
            self.assertEqual(result.stats.pages_fetched, 2)
            self.assertEqual(result.stats.distinct_domains, 2)
            self.assertIn("[S1]", result.answer_markdown)
            self.assertTrue(updates)
            evidence_updates = [message for _, message in updates if message.startswith("Found:")]
            self.assertTrue(evidence_updates)
            self.assertIn("The verified value is 42.", evidence_updates[0])
            self.assertIn("\nCoverage ", evidence_updates[0])
            store.close()

    async def test_model_latency_does_not_consume_browsing_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "test.sqlite3")
            controller = ResearchController(
                search=FakeSearch(),
                reader=FakeReader(),
                agent=ResearchAgent(SlowModel()),
                store=store,
            )
            started = time.monotonic()

            result = await controller.run(
                "What is the value?",
                effort="quick",
                freshness=None,
                budget=Budget(0.03, 1, 1, 1, 1, 0.1),
            )

            self.assertGreater(time.monotonic() - started, 0.12)
            self.assertEqual(result.stats.search_queries, 1)
            self.assertEqual(result.stats.pages_fetched, 1)
            self.assertEqual(result.stop_reason, "page_budget_exhausted")
            self.assertLess(result.stats.browsing_elapsed_ms, 30)
            self.assertFalse(any("TimeoutError" in warning for warning in result.warnings))
            store.close()

    async def test_browsing_budget_interrupts_a_slow_search(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "test.sqlite3")
            controller = ResearchController(
                search=SlowSearch(),
                reader=FakeReader(),
                agent=ResearchAgent(FakeModel()),
                store=store,
            )
            started = time.monotonic()

            result = await controller.run(
                "What is the value?",
                effort="quick",
                freshness=None,
                budget=Budget(0.03, 1, 1, 1, 1, 0.1),
            )

            self.assertLess(time.monotonic() - started, 0.5)
            self.assertEqual(result.stop_reason, "time_budget_exhausted")
            self.assertEqual(result.stats.pages_fetched, 0)
            self.assertGreaterEqual(result.stats.browsing_elapsed_ms, 25)
            self.assertTrue(any("TimeoutError" in warning for warning in result.warnings))
            store.close()

    async def test_prefetch_overlaps_page_retrieval_without_parallel_model_calls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "test.sqlite3")
            reader = ConcurrentReader()
            controller = ResearchController(
                search=FakeSearch(),
                reader=reader,
                agent=ResearchAgent(FakeModel()),
                store=store,
                prefetch_pages=2,
            )

            result = await controller.run(
                "What is the verified value?",
                effort="auto",
                freshness=None,
                budget=Budget(20, 2, 3, 2, 1, 0.05),
            )

            self.assertEqual(reader.maximum_active, 2)
            self.assertGreaterEqual(result.stats.prefetch_started, 2)
            self.assertTrue(result.coverage.sufficient)
            store.close()

    async def test_empty_specialized_lane_retries_general_web_search(self) -> None:
        class EmptyLaneSearch:
            def __init__(self) -> None:
                self.categories: list[str | None] = []

            async def search(self, *_args, **kwargs):
                category = kwargs.get("categories")
                self.categories.append(category)
                if category:
                    return []
                return [
                    SearchResult(
                        url="https://paper.example/result",
                        title="Result",
                        snippet="Relevant paper",
                        rank=1,
                    )
                ]

        search = EmptyLaneSearch()
        results, fell_back = await _search_with_lane(
            search,
            _ActiveTimeBudget(1),
            "relevant paper",
            SearchLane.ACADEMIC,
            None,
            None,
        )

        self.assertTrue(fell_back)
        self.assertEqual(search.categories, ["science", None])
        self.assertEqual(len(results), 1)

    def test_discovers_only_relevant_same_site_links(self) -> None:
        document = Document(
            url="https://docs.example/start",
            final_url="https://docs.example/start",
            title="Start",
            content="Entry point",
            method="fixture",
            links=[
                "https://docs.example/releases/version-4",
                "https://docs.example/account/login",
                "https://other.example/releases/version-4",
            ],
        )
        spec = ResearchSpec(
            original_query="What changed in version 4?",
            task_type=TaskType.FACT,
            requirements=[Requirement(id="R1", question="What changed in version 4?")],
        )

        candidates = _linked_candidates(document, spec, ["R1"], set())

        self.assertEqual(
            [item.url for item in candidates], ["https://docs.example/releases/version-4"]
        )


if __name__ == "__main__":
    unittest.main()
