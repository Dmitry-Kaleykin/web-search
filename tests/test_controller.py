from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from web_research.agent import ResearchAgent
from web_research.config import Budget
from web_research.controller import ResearchController
from web_research.models import Document, SearchResult
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
                        "primary_required": False,
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


class DateCapturingModel(FakeModel):
    def __init__(self) -> None:
        self.calls = []

    async def complete_json(self, **kwargs):
        self.calls.append(kwargs)
        return await super().complete_json(**kwargs)


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_model_stage_receives_authoritative_current_date(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "test.sqlite3")
            model = DateCapturingModel()
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
                budget=Budget(20, 1, 1, 1, 1, 0.1),
            )

            self.assertEqual(
                [call["schema_name"] for call in model.calls],
                [
                    "research_spec",
                    "search_queries",
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
            self.assertEqual(result.stats.independent_domains, 2)
            self.assertIn("[S1]", result.answer_markdown)
            self.assertTrue(updates)
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


if __name__ == "__main__":
    unittest.main()
