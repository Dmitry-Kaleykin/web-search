from __future__ import annotations

import unittest
from pathlib import Path

from web_research.evaluation import evaluate_directory


class EvaluationFixtureTests(unittest.TestCase):
    def test_bundled_replay_fixtures_pass(self) -> None:
        fixture_dir = Path(__file__).resolve().parents[1] / "eval" / "fixtures"
        results = evaluate_directory(fixture_dir)

        self.assertGreaterEqual(len(results), 2)
        self.assertTrue(all(result.passed for result in results), results)


if __name__ == "__main__":
    unittest.main()
