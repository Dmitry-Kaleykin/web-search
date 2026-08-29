from __future__ import annotations

import unittest

from web_research.models import Requirement, ResearchSpec, SearchResult, TaskType
from web_research.ranking import gate_candidates


class RelevanceGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = ResearchSpec(
            original_query="Investigate VPN protocol incompatibilities",
            task_type=TaskType.FACT,
            requirements=[
                Requirement(
                    id="R1",
                    question="Which VPN protocols have compatibility problems?",
                )
            ],
        )
        self.relevant = SearchResult(
            url="https://vpn.example/protocol-compatibility",
            title="VPN protocol compatibility matrix",
            snippet="Known WireGuard and OpenVPN incompatibilities",
            rank=2,
        )
        self.noise = SearchResult(
            url="https://news.example/new-york",
            title="The latest news from New York",
            snippet="Culture, restaurants and city reporting",
            rank=1,
        )

    def test_raw_semantic_score_rejects_high_ranked_noise(self) -> None:
        result = gate_candidates(
            [self.noise, self.relevant],
            search_query="VPN protocol incompatibilities",
            spec=self.spec,
            uncovered_requirement_ids=["R1"],
            semantic_scores={self.noise.url: 0.002, self.relevant.url: 0.91},
            semantic_min_score=0.08,
            semantic_relative_ratio=0.15,
            lexical_min_score=0.01,
            rejected_batch_streak=0,
        )

        self.assertEqual(result.accepted, [self.relevant])
        self.assertEqual([item.url for item, _ in result.rejected], [self.noise.url])
        self.assertEqual(result.mode, "semantic")

    def test_floor_relaxes_before_low_confidence_probe(self) -> None:
        initial = gate_candidates(
            [self.relevant],
            search_query="obscure protocol issue",
            spec=self.spec,
            uncovered_requirement_ids=["R1"],
            semantic_scores={self.relevant.url: 0.05},
            semantic_min_score=0.08,
            semantic_relative_ratio=0.15,
            lexical_min_score=0.01,
            rejected_batch_streak=0,
        )
        relaxed = gate_candidates(
            [self.relevant],
            search_query="obscure protocol issue",
            spec=self.spec,
            uncovered_requirement_ids=["R1"],
            semantic_scores={self.relevant.url: 0.05},
            semantic_min_score=0.08,
            semantic_relative_ratio=0.15,
            lexical_min_score=0.01,
            rejected_batch_streak=1,
        )

        self.assertEqual(initial.accepted, [])
        self.assertEqual(relaxed.accepted, [self.relevant])

    def test_lexical_fallback_only_rejects_near_zero_overlap(self) -> None:
        result = gate_candidates(
            [self.noise, self.relevant],
            search_query="VPN protocol incompatibilities",
            spec=self.spec,
            uncovered_requirement_ids=["R1"],
            semantic_scores={},
            semantic_min_score=0.08,
            semantic_relative_ratio=0.15,
            lexical_min_score=0.01,
            rejected_batch_streak=0,
        )

        self.assertEqual(result.accepted, [self.relevant])
        self.assertEqual([item.url for item, _ in result.rejected], [self.noise.url])
        self.assertEqual(result.mode, "lexical")


if __name__ == "__main__":
    unittest.main()
