from __future__ import annotations

import unittest

from web_research.models import TaskType
from web_research.pipeline import (
    EvidenceStrategy,
    FollowupStrategy,
    QueryStrategy,
    RerankingStrategy,
    SpecStrategy,
    pipeline_for_effort,
    pipeline_for_request,
)


class PipelineProfileTests(unittest.TestCase):
    def test_quick_assembles_only_low_latency_stages(self) -> None:
        pipeline = pipeline_for_effort("quick")

        self.assertEqual(pipeline.name, "quick")
        self.assertEqual(pipeline.spec, SpecStrategy.HEURISTIC)
        self.assertEqual(pipeline.queries, QueryStrategy.DIRECT)
        self.assertEqual(pipeline.reranking, RerankingStrategy.DETERMINISTIC)
        self.assertEqual(pipeline.evidence, EvidenceStrategy.HEURISTIC)
        self.assertEqual(pipeline.followups, FollowupStrategy.NONE)
        self.assertFalse(pipeline.discover_links)
        self.assertEqual(pipeline.budget.max_searches, 1)
        self.assertEqual(pipeline.budget.max_pages, 2)

    def test_auto_routes_simple_facts_to_quick(self) -> None:
        pipeline = pipeline_for_request("auto", "Who is Ada Lovelace?", None)

        self.assertEqual(pipeline.name, "quick")

    def test_auto_routes_broader_tasks_to_standard(self) -> None:
        for task_type in (
            TaskType.COMPARISON,
            TaskType.RECOMMENDATION,
            TaskType.CURRENT_EVENT,
            TaskType.EXPLANATION,
            TaskType.EXPLORATION,
        ):
            with self.subTest(task_type=task_type):
                pipeline = pipeline_for_effort("auto", inferred_task_type=task_type)
                self.assertEqual(pipeline.name, "standard")

    def test_auto_keeps_fresh_facts_on_standard_pipeline(self) -> None:
        pipeline = pipeline_for_request("auto", "Who is the current prime minister?", "current")

        self.assertEqual(pipeline.name, "standard")

    def test_thorough_owns_breadth_requirements(self) -> None:
        pipeline = pipeline_for_effort("thorough")

        self.assertEqual(pipeline.min_searches, 3)
        self.assertEqual(pipeline.min_usable_domains, 6)

    def test_unknown_effort_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unknown effort level"):
            pipeline_for_effort("unbounded")


if __name__ == "__main__":
    unittest.main()
