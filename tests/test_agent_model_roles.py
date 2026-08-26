from __future__ import annotations

import unittest
from unittest.mock import AsyncMock

from web_research.agent import ResearchAgent
from web_research.models import Document, PlannedQuery, Requirement, ResearchSpec, TaskType


class ResearchAgentModelRoleTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_document_analysis_uses_dedicated_evidence_model(self) -> None:
        main_model = AsyncMock()
        main_model.complete_json.return_value = {"queries": ["planned query"]}
        evidence_model = AsyncMock()
        evidence_model.complete_json.return_value = {
            "source_class": "independent",
            "claims": [],
        }
        agent = ResearchAgent(main_model, evidence_model=evidence_model)
        spec = ResearchSpec(
            original_query="Question",
            task_type=TaskType.FACT,
            requirements=[Requirement(id="R1", question="What happened?")],
        )
        document = Document(
            url="https://example.test/article",
            final_url="https://example.test/article",
            title="Article",
            content="This is a sufficiently detailed article paragraph about what happened.",
            method="http",
        )

        queries = await agent.plan_queries(spec)
        await agent.analyze_document(spec, document)

        self.assertEqual(queries, [PlannedQuery(query="planned query")])
        self.assertEqual(main_model.complete_json.await_count, 1)
        self.assertEqual(
            main_model.complete_json.await_args.kwargs["schema_name"], "search_queries"
        )
        self.assertEqual(evidence_model.complete_json.await_count, 1)
        self.assertEqual(
            evidence_model.complete_json.await_args.kwargs["schema_name"], "source_evidence"
        )
        self.assertEqual(agent.evidence_model_usage()["model"], "pi-active")


if __name__ == "__main__":
    unittest.main()
