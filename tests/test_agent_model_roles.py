from __future__ import annotations

import unittest
from unittest.mock import AsyncMock

from web_research.agent import ResearchAgent
from web_research.evidence import EvidenceBatch, EvidenceLedger
from web_research.models import Document, SourceClass


class ResearchAgentModelRoleTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_research_stages_use_the_main_model(self) -> None:
        model = AsyncMock()
        statement = "The product supports offline mode."
        claim = {
            "requirement_id": "R1",
            "statement": statement,
            "excerpt": statement,
            "confidence": 0.9,
        }
        model.complete_json.side_effect = [
            {
                "task_type": "fact",
                "requirements": [{"id": "R1", "question": "Does it work offline?"}],
            },
            {"queries": ["product offline mode"]},
            {"source_class": "primary", "claims": [claim]},
            {"stop": True},
            {"answer_markdown": "The product supports offline mode. [S1]"},
        ]
        agent = ResearchAgent(model)
        spec = await agent.compile_spec("Does the product work offline?", None)
        queries = await agent.plan_queries(spec)
        document = Document(
            url="https://example.test/product",
            final_url="https://example.test/product",
            title="Product",
            content=statement,
            method="http",
        )
        batch = await agent.analyze_document(spec, document)
        self.assertEqual(batch, EvidenceBatch(source_class=SourceClass.PRIMARY, claims=[claim]))
        ledger = EvidenceLedger(spec)
        ledger.add_document(document, batch)
        await agent.assess(spec, ledger)
        answer = await agent.synthesize(spec, ledger)

        self.assertEqual(queries[0].query, "product offline mode")
        self.assertIn("The product supports offline mode. [S1]", answer)
        self.assertEqual(
            [call.kwargs["schema_name"] for call in model.complete_json.await_args_list],
            [
                "research_spec",
                "search_queries",
                "source_evidence",
                "sufficiency_assessment",
                "research_answer",
            ],
        )


if __name__ == "__main__":
    unittest.main()
