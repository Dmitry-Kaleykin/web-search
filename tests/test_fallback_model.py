from __future__ import annotations

import unittest
from unittest.mock import AsyncMock

from web_research.model.fallback import FallbackModelClient


class FallbackModelClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_falls_back_and_disables_preferred_model_after_repeated_failures(self) -> None:
        preferred = AsyncMock()
        preferred.complete_json.side_effect = RuntimeError("reader unavailable")
        fallback = AsyncMock()
        fallback.complete_json.return_value = {"answer": "dynamic"}
        model = FallbackModelClient(preferred, fallback, disable_after_failures=2)

        arguments = {
            "system": "System",
            "user": "User",
            "schema_name": "source_evidence",
            "schema": {"type": "object"},
        }
        first = await model.complete_json(**arguments)
        second = await model.complete_json(**arguments)
        third = await model.complete_json(**arguments)

        self.assertEqual(first, {"answer": "dynamic"})
        self.assertEqual(second, {"answer": "dynamic"})
        self.assertEqual(third, {"answer": "dynamic"})
        self.assertEqual(preferred.complete_json.await_count, 2)
        self.assertEqual(fallback.complete_json.await_count, 3)
        self.assertTrue(model.disabled)
        self.assertEqual(
            model.usage(),
            {
                "attempts": 2,
                "successes": 0,
                "failures": 2,
                "fallbacks": 3,
                "disabled": True,
            },
        )

    async def test_successful_preferred_call_does_not_use_fallback(self) -> None:
        preferred = AsyncMock()
        preferred.complete_json.return_value = {"answer": "fast"}
        fallback = AsyncMock()
        model = FallbackModelClient(preferred, fallback)

        result = await model.complete_json(
            system="System",
            user="User",
            schema_name="source_evidence",
            schema={"type": "object"},
        )

        self.assertEqual(result, {"answer": "fast"})
        fallback.complete_json.assert_not_awaited()
        self.assertEqual(model.usage()["attempts"], 1)
        self.assertEqual(model.usage()["successes"], 1)
        self.assertEqual(model.usage()["fallbacks"], 0)


if __name__ == "__main__":
    unittest.main()
