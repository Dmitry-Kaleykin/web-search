from __future__ import annotations

import unittest

from web_research.evidence import EvidenceBatch, EvidenceLedger
from web_research.models import (
    Document,
    Requirement,
    ResearchSpec,
    SourceClass,
    TaskType,
)


class EvidenceLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = ResearchSpec(
            original_query="Compare A and B",
            task_type=TaskType.COMPARISON,
            requirements=[
                Requirement(
                    id="R1",
                    question="What are the official dimensions?",
                    min_sources=1,
                    primary_required=True,
                ),
                Requirement(
                    id="R2",
                    question="How reliable is it in real use?",
                    min_sources=2,
                ),
            ],
        )

    def test_primary_and_independent_source_rules(self) -> None:
        ledger = EvidenceLedger(self.spec)
        first = Document(
            url="https://maker.example/specs",
            final_url="https://maker.example/specs",
            title="Official specifications",
            content=(
                "The product measures exactly 10 by 20 centimetres. Users report good reliability."
            ),
            method="http",
        )
        ledger.add_document(
            first,
            EvidenceBatch(
                SourceClass.PRIMARY,
                [
                    {
                        "requirement_id": "R1",
                        "statement": "It measures 10 by 20 centimetres.",
                        "excerpt": "The product measures exactly 10 by 20 centimetres.",
                        "confidence": 0.9,
                        "stance": "supports",
                    },
                    {
                        "requirement_id": "R2",
                        "statement": "Users report good reliability.",
                        "excerpt": "Users report good reliability.",
                        "confidence": 0.8,
                        "stance": "supports",
                    },
                ],
            ),
        )
        coverage = ledger.coverage()
        self.assertTrue(coverage.items[0].covered)
        self.assertFalse(coverage.items[1].covered)
        self.assertFalse(coverage.sufficient)

        second = Document(
            url="https://reviews.example/product",
            final_url="https://reviews.example/product",
            title="Independent review",
            content="Our long-term test found the product remained reliable.",
            method="http",
        )
        ledger.add_document(
            second,
            EvidenceBatch(
                SourceClass.INDEPENDENT,
                [
                    {
                        "requirement_id": "R2",
                        "statement": "A long-term test found it reliable.",
                        "excerpt": "Our long-term test found the product remained reliable.",
                        "confidence": 0.85,
                        "stance": "supports",
                    }
                ],
            ),
        )
        coverage = ledger.coverage()
        self.assertTrue(coverage.sufficient)
        self.assertEqual(coverage.score, 1.0)


if __name__ == "__main__":
    unittest.main()
