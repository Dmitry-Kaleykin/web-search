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
                    question="What are the dimensions?",
                    min_sources=1,
                ),
                Requirement(
                    id="R2",
                    question="How reliable is it in real use?",
                    min_sources=2,
                ),
            ],
        )

    def test_source_count_and_domain_rules(self) -> None:
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

    def test_source_class_is_descriptive_metadata_only(self) -> None:
        spec = ResearchSpec(
            original_query="Latest Qwen release",
            task_type=TaskType.CURRENT_EVENT,
            subjects=["Qwen"],
            requirements=[
                Requirement(
                    id="R1",
                    question="What is the latest Qwen release?",
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
        self.assertEqual(source.source_class, SourceClass.PRIMARY)
        self.assertTrue(ledger.coverage().sufficient)

    def test_conflicting_structured_values_block_required_coverage(self) -> None:
        spec = ResearchSpec(
            original_query="What is the current price?",
            task_type=TaskType.CURRENT_EVENT,
            requirements=[Requirement(id="R1", question="What is the price?", min_sources=2)],
        )
        ledger = EvidenceLedger(spec)
        for domain, value in (("one.example", "$49"), ("two.example", "$59")):
            document = Document(
                url=f"https://{domain}/price",
                final_url=f"https://{domain}/price",
                title="Price",
                content=f"The current price is {value}.",
                method="http",
            )
            ledger.add_document(
                document,
                EvidenceBatch(
                    SourceClass.INDEPENDENT,
                    [
                        {
                            "requirement_id": "R1",
                            "statement": f"The current price is {value}.",
                            "excerpt": f"The current price is {value}.",
                            "confidence": 0.9,
                            "stance": "supports",
                            "value_kind": "price",
                            "normalized_value": value,
                        }
                    ],
                ),
            )

        coverage = ledger.coverage()
        self.assertFalse(coverage.sufficient)
        self.assertFalse(coverage.items[0].covered)
        self.assertIn("conflicting price values", coverage.conflicts[0])

    def test_equivalent_structured_value_formatting_does_not_create_conflict(self) -> None:
        spec = ResearchSpec(
            original_query="What is the current price?",
            task_type=TaskType.CURRENT_EVENT,
            requirements=[Requirement(id="R1", question="What is the price?", min_sources=2)],
        )
        ledger = EvidenceLedger(spec)
        for domain, shown, normalized in (
            ("one.example", "$49", "$49"),
            ("two.example", "49 USD", "49 USD"),
        ):
            document = Document(
                url=f"https://{domain}/price",
                final_url=f"https://{domain}/price",
                title="Price",
                content=f"The current price is {shown}.",
                method="http",
            )
            ledger.add_document(
                document,
                EvidenceBatch(
                    SourceClass.INDEPENDENT,
                    [
                        {
                            "requirement_id": "R1",
                            "statement": f"The current price is {shown}.",
                            "excerpt": f"The current price is {shown}.",
                            "confidence": 0.9,
                            "stance": "supports",
                            "value_kind": "price",
                            "normalized_value": normalized,
                        }
                    ],
                ),
            )

        coverage = ledger.coverage()
        self.assertTrue(coverage.sufficient)
        self.assertEqual(coverage.conflicts, [])

    def test_fresh_requirement_counts_only_dated_sources(self) -> None:
        spec = ResearchSpec(
            original_query="What is the latest release?",
            task_type=TaskType.CURRENT_EVENT,
            requirements=[
                Requirement(
                    id="R1",
                    question="What is the latest release?",
                    freshness_required=True,
                )
            ],
        )
        ledger = EvidenceLedger(spec)
        document = Document(
            url="https://news.example/release",
            final_url="https://news.example/release",
            title="Release",
            content="Version 4 was released.",
            method="http",
        )
        ledger.add_document(
            document,
            EvidenceBatch(
                SourceClass.NEWS,
                [
                    {
                        "requirement_id": "R1",
                        "statement": "Version 4 was released.",
                        "excerpt": "Version 4 was released.",
                        "confidence": 0.9,
                        "stance": "supports",
                    }
                ],
            ),
        )
        self.assertFalse(ledger.coverage().sufficient)
        ledger.sources[0].published_at = "2026-08-25"
        self.assertTrue(ledger.coverage().sufficient)

    def test_requirement_dependency_blocks_downstream_coverage(self) -> None:
        spec = ResearchSpec(
            original_query="Find a dependent fact",
            task_type=TaskType.EXPLORATION,
            requirements=[
                Requirement(id="R1", question="Which entity?"),
                Requirement(id="R2", question="What did it release?", depends_on=["R1"]),
            ],
        )
        ledger = EvidenceLedger(spec)
        document = Document(
            url="https://two.example/release",
            final_url="https://two.example/release",
            title="Release",
            content="The entity released version 4.",
            method="http",
        )
        ledger.add_document(
            document,
            EvidenceBatch(
                SourceClass.NEWS,
                [
                    {
                        "requirement_id": "R2",
                        "statement": "The entity released version 4.",
                        "excerpt": "The entity released version 4.",
                        "confidence": 0.9,
                        "stance": "supports",
                    }
                ],
            ),
        )
        coverage = ledger.coverage()
        self.assertFalse(coverage.items[1].covered)
        self.assertIn("R1", coverage.items[1].reason)


if __name__ == "__main__":
    unittest.main()
