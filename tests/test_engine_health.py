"""Engine health distinguishes a local fault from an upstream refusal."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from web_research.search.health import (
    EngineHealth,
    failure_delay,
    local_fault,
    upstream_declared_wait,
)
from web_research.storage import SQLiteStore


class FailureClassificationTests(unittest.TestCase):
    def test_local_transport_and_policy_reasons_are_short_waits(self):
        for reason in (
            "SSL connect error",
            "Could not resolve host",
            "Blocked by the local network policy: method PUT is not allowed",
            "Response exceeds the native byte limit",
        ):
            with self.subTest(reason=reason):
                self.assertTrue(local_fault(reason))
                self.assertLessEqual(failure_delay(reason), 120)

    def test_upstream_refusals_keep_their_own_windows(self):
        self.assertTrue(upstream_declared_wait("CAPTCHA challenge"))
        self.assertGreaterEqual(failure_delay("CAPTCHA challenge"), 86400)
        self.assertGreaterEqual(failure_delay("too many requests"), 3600)
        self.assertFalse(local_fault("too many requests"))

    def test_an_unnamed_reason_is_not_treated_as_a_suspension(self):
        self.assertFalse(local_fault("parsing error"))
        self.assertFalse(upstream_declared_wait("parsing error"))
        self.assertEqual(failure_delay("parsing error"), 300)


class EngineHealthTests(unittest.IsolatedAsyncioTestCase):
    def _health(self) -> EngineHealth:
        clock = {"now": 1_000.0}
        health = EngineHealth(None, clock=lambda: clock["now"])
        health._clock_box = clock  # type: ignore[attr-defined]
        return health

    def _advance(self, health: EngineHealth, seconds: float) -> None:
        health._clock_box["now"] += seconds  # type: ignore[attr-defined]

    def test_a_never_verified_engine_is_probed_again_soon(self):
        """A broken integration must not disappear behind a day of untested silence."""
        health = self._health()
        for attempt in range(6):
            if attempt:
                self._advance(health, 3600)
            health.failed("duckduckgo web", "parsing error")
        status = health.snapshot(["duckduckgo web"])["duckduckgo web"]
        self.assertIsNotNone(status["retry_at"])
        self.assertLessEqual(status["retry_at"] - health.clock(), 3600)
        self.assertEqual(status["fault"], "upstream")
        self.assertGreaterEqual(status["consecutive_failures"], 5)

    def test_a_local_fault_never_backs_off_past_half_an_hour(self):
        health = self._health()
        for attempt in range(9):
            if attempt:
                self._advance(health, 1800)
            health.failed("crowdview", "SSL connect error")
        status = health.snapshot(["crowdview"])["crowdview"]
        self.assertLessEqual(status["retry_at"] - health.clock(), 1800)
        self.assertEqual(status["fault"], "local_transport")
        self.assertEqual(status["reason"], "SSL connect error")

    def test_upstream_windows_survive_escalation_for_an_unverified_engine(self):
        health = self._health()
        health.failed("mojeek", "access denied")
        status = health.snapshot(["mojeek"])["mojeek"]
        self.assertGreaterEqual(status["retry_at"] - health.clock(), 86400)

    def test_success_clears_the_reason_and_its_classification(self):
        health = self._health()
        health.failed("mojeek", "SSL connect error")
        health.succeeded("mojeek", 5)
        status = health.snapshot(["mojeek"])["mojeek"]
        self.assertEqual(status["status"], "working")
        self.assertIsNone(status["fault"])
        self.assertEqual(status["successes"], 1)

    def test_reset_forgets_evidence_about_a_policy_that_no_longer_exists(self):
        health = self._health()
        health.failed("mojeek", "access denied")
        self.assertEqual(health.reset(), 1)
        self.assertEqual(health.snapshot(["mojeek"])["mojeek"]["status"], "untested")


class StoredEngineHealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_reset_clears_both_engine_wait_tables(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "research.sqlite3")
            health = EngineHealth(store)
            health.failed("brave", "too many requests")
            store.record_engine_cooldown("legacy", "rate limited", time.time() + 600)
            self.assertEqual(store.engine_records()["brave"]["reason"], "too many requests")
            self.assertEqual(health.reset(), 2)
            self.assertEqual(store.engine_records(), {})
            self.assertEqual(store.active_engine_cooldowns(), {})
            store.close()
