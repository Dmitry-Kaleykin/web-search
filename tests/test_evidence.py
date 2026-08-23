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
            subjects=["Maker"],
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

    def test_rejects_claim_with_unsupported_numbers_and_dates(self) -> None:
        ledger = EvidenceLedger(self.spec)
        document = Document(
            url="https://maker.example/report",
            final_url="https://maker.example/report",
            title="Annual report",
            content="Revenue was 10 million in 2025.",
            method="http",
        )

        source, added = ledger.add_document(
            document,
            EvidenceBatch(
                SourceClass.PRIMARY,
                [
                    {
                        "requirement_id": "R1",
                        "statement": "Revenue was 99 million in 2026.",
                        "excerpt": "not a verbatim excerpt",
                        "confidence": 0.95,
                        "stance": "supports",
                    }
                ],
            ),
        )

        self.assertEqual(added, 0)
        self.assertEqual(ledger.claims, [])
        self.assertIn("unsupported_claim_rejected:R1", source.warnings)

    def test_accepts_supported_paraphrase_but_caps_replaced_excerpt_confidence(self) -> None:
        ledger = EvidenceLedger(self.spec)
        document = Document(
            url="https://maker.example/report",
            final_url="https://maker.example/report",
            title="Annual report",
            content="The company reported revenue of 10 million for 2025.",
            method="http",
        )

        source, added = ledger.add_document(
            document,
            EvidenceBatch(
                SourceClass.PRIMARY,
                [
                    {
                        "requirement_id": "R1",
                        "statement": "Revenue was 10 million in 2025.",
                        "excerpt": "not a verbatim excerpt",
                        "confidence": 0.95,
                        "stance": "supports",
                    }
                ],
            ),
        )

        self.assertEqual(added, 1)
        self.assertEqual(ledger.claims[0].confidence, 0.65)
        self.assertIn("non_verbatim_excerpt_replaced:R1", source.warnings)

    def test_downgrades_obviously_non_primary_domain(self) -> None:
        ledger = EvidenceLedger(self.spec)
        document = Document(
            url="https://en.wikipedia.org/wiki/Maker",
            final_url="https://en.wikipedia.org/wiki/Maker",
            title="Maker",
            content="The product measures exactly 10 by 20 centimetres.",
            method="http",
        )

        source, added = ledger.add_document(
            document,
            EvidenceBatch(
                SourceClass.PRIMARY,
                [
                    {
                        "requirement_id": "R1",
                        "statement": "It measures 10 by 20 centimetres.",
                        "excerpt": "The product measures exactly 10 by 20 centimetres.",
                        "confidence": 0.9,
                        "stance": "supports",
                    }
                ],
            ),
        )

        self.assertEqual(added, 1)
        self.assertEqual(source.source_class, SourceClass.INDEPENDENT)
        self.assertIn("source_class_downgraded:primary_to_independent", source.warnings)
        self.assertFalse(ledger.coverage().items[0].has_primary)

    def test_downgrades_subject_named_third_party_domain(self) -> None:
        spec = ResearchSpec(
            original_query="Latest Qwen release",
            task_type=TaskType.CURRENT_EVENT,
            subjects=["Qwen"],
            requirements=[
                Requirement(
                    id="R1",
                    question="What is the latest Qwen release?",
                    primary_required=True,
                )
            ],
        )
        ledger = EvidenceLedger(spec)
        document = Document(
            url="https://docs.qwencloud.com/changelog/models",
            final_url="https://docs.qwencloud.com/changelog/models",
            title="Qwen model releases",
            content="Qwen 3.8 was released in August 2026.",
            method="http",
        )

        source, added = ledger.add_document(
            document,
            EvidenceBatch(
                SourceClass.PRIMARY,
                [
                    {
                        "requirement_id": "R1",
                        "statement": "Qwen 3.8 was released in August 2026.",
                        "excerpt": "Qwen 3.8 was released in August 2026.",
                        "confidence": 0.9,
                        "stance": "supports",
                    }
                ],
            ),
        )

        self.assertEqual(added, 1)
        self.assertEqual(source.source_class, SourceClass.UNKNOWN)
        self.assertIn("source_class_downgraded:primary_to_unknown", source.warnings)
        self.assertFalse(ledger.coverage().sufficient)


if __name__ == "__main__":
    unittest.main()
