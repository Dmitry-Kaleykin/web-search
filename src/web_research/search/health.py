"""Observed access health; operational waits never measure research completeness."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from typing import Any

from ..storage import SQLiteStore

# Reasons that describe this client's own transport or policy rather than the upstream site's
# opinion of it. Treating a local fault as an upstream suspension hides a broken integration
# behind an engine outage, which is the worst combination to debug: the engine is never retried
# and the reason keeps the wording that made it look like someone else's problem.
LOCAL_FAULT_TERMS = (
    "network policy",
    "local network",
    "transfer limit",
    "byte limit",
    "ssl connect",
    "certificate",
    "could not resolve",
    "dns",
    "cancelled",
    "saturated",
    "no result",
    "proxy",
)
UPSTREAM_WAIT_TERMS = (
    "recaptcha",
    "captcha",
    "access denied",
    "blocked",
    "suspended",
    "too many",
    "rate limit",
    "403",
    "429",
)


def local_fault(reason: str) -> bool:
    text = reason.casefold()
    return any(term in text for term in LOCAL_FAULT_TERMS)


# The upstream said how long it intends to keep saying no, either in these words or in a
# Retry-After this caller was handed. Its own window is honoured even for an engine that has
# never answered; only a reason nobody named gets the shorter never-verified retry window.
def upstream_declared_wait(reason: str) -> bool:
    text = reason.casefold()
    return any(term in text for term in UPSTREAM_WAIT_TERMS)


def failure_delay(reason: str) -> float:
    text = reason.casefold()
    if local_fault(text):
        # Short by design: the fault is on this side, so keep probing and keep reporting it.
        return 120
    # Match the bundled SearXNG suspension policy. Remote instances may differ; these
    # are conservative retry estimates, not claims about the upstream unblock time.
    if "recaptcha" in text:
        return 7 * 86400
    if "cloudflare" in text and "captcha" in text:
        return 15 * 86400
    if any(term in text for term in ("429", "too many", "rate limit")):
        return 3600
    if any(term in text for term in ("captcha", "access denied", "403", "blocked")):
        return 86400
    if any(term in text for term in ("timeout", "timed out", "connection", "reset", "503")):
        return 120
    return 300


class EngineHealth:
    def __init__(self, store: SQLiteStore | None, *, clock: Callable = time.time):
        self.store = store
        self.clock = clock
        self.local: dict[str, dict] = {}

    def records(self) -> dict[str, dict]:
        return self.store.engine_records() if self.store else self.local

    def update(self, engine: str, action: Callable) -> dict:
        if self.store:
            return self.store.update_engine(engine, action)
        record = self.local.setdefault(
            engine, {"engine": engine, "reason": "", "expires_at": 0, "updated_at": 0, "state": {}}
        )
        action(record)
        return record

    def failed(self, engine: str, reason: str, retry_after: float | None = None) -> None:
        now = self.clock()

        def change(record):
            state = record["state"]
            previous = state.get("consecutive_failures", int(bool(record["reason"])))
            suspended = "suspended" in reason.casefold()
            count = max(1, previous) if suspended else previous + 1
            base = failure_delay(reason)
            # Repeated observed failures back off. A suspension echo is not a new attempt.
            # An engine that has never answered at all must not sleep for a day: "never worked
            # here" is the state a reader needs soon, and a long cooldown turns a broken
            # integration into an unexplained absence from the fleet.
            ceiling = 1800 if local_fault(reason) else max(base, 7 * 86400)
            if (
                not local_fault(reason)
                and not upstream_declared_wait(reason)
                and not state.get("successes")
            ):
                ceiling = min(ceiling, 3600)
            delay = min(base * (2 ** min(count - 1, 8)), ceiling)
            delay = max(delay, retry_after or 0)
            expiry = max(record["expires_at"], now + delay)
            state.update(
                consecutive_failures=count,
                failures=state.get("failures", 0) + int(not suspended),
                suspension_reports=state.get("suspension_reports", 0) + int(suspended),
                last_failure=now,
                delay_seconds=delay,
                lease_until=0,
                lease_token="",
            )
            record.update(reason=reason, expires_at=expiry, updated_at=now)

        self.update(engine, change)

    def succeeded(
        self,
        engine: str,
        count: int,
        latency_ms: float | None = None,
        *,
        started_at: float | None = None,
    ) -> None:
        now = self.clock()

        def change(record):
            state = record["state"]
            if started_at is not None and state.get("last_failure", 0) > started_at:
                # A slow successful response cannot erase a newer failure in another process.
                return
            state.update(
                successes=state.get("successes", 0) + 1,
                consecutive_failures=0,
                last_success=now,
                last_result_count=count,
                last_latency_ms=latency_ms,
                lease_until=0,
                lease_token="",
            )
            record.update(reason="", expires_at=0, updated_at=now)

        self.update(engine, change)

    def reset(self) -> int:
        """Forget observed waits, because the thing that produced them has changed.

        Engine health is evidence about a specific backend build, engine set and network policy.
        Rebuilding the image or changing the policy invalidates it, and a stale multi-day cooldown
        would keep an engine absent for a fault nobody can now reproduce.
        """
        if not self.store:
            count = len(self.local)
            self.local.clear()
            return count
        return self.store.clear_engine_health()

    def claim(self, engine: str, seconds: float) -> str | None:
        """Only one process can test a recovering engine at a time; leases expire after crashes."""
        now = self.clock()
        token = uuid.uuid4().hex

        def change(record):
            state = record["state"]
            if record["expires_at"] > now or state.get("lease_until", 0) > now:
                return
            state.update(lease_token=token, lease_until=now + seconds)
            record["updated_at"] = now

        record = self.update(engine, change)
        return token if record["state"].get("lease_token") == token else None

    def release(self, engine: str, token: str, *, inconclusive: bool = False) -> None:
        now = self.clock()

        def change(record):
            state = record["state"]
            if state.get("lease_token") != token:
                return
            state.update(lease_token="", lease_until=0)
            if inconclusive:
                # No verified engine execution (e.g. unsupported filters). Not a failure,
                # but avoid probing again on every next query.
                record["expires_at"] = max(record["expires_at"], now + 120)

        self.update(engine, change)

    def snapshot(self, engines: list[str]) -> dict[str, dict[str, Any]]:
        records, now = self.records(), self.clock()
        output = {}
        for engine in engines:
            record = records.get(engine, {})
            state = record.get("state", {})
            failures = state.get("consecutive_failures", int(bool(record.get("reason"))))
            lease = state.get("lease_until", 0)
            until = record.get("expires_at", 0)
            success = state.get("last_success")
            status = "untested"
            if success:
                status = "working" if now - success <= 86400 else "unverified"
            if failures:
                status = "recovery_due"
            if until > now:
                status = "cooling_down"
            if lease > now:
                status = "recovering"
            output[engine] = {
                "status": status,
                "reason": record.get("reason", ""),
                "fault": (
                    "local_transport"
                    if local_fault(record.get("reason", ""))
                    else "upstream"
                    if record.get("reason")
                    else None
                ),
                "consecutive_failures": failures,
                "repeated_failures": failures > 1,
                "successes": state.get("successes", 0),
                "failures": state.get("failures", 0),
                "last_success": success,
                "last_failure": state.get("last_failure"),
                "last_latency_ms": state.get("last_latency_ms"),
                "retry_at": max(until, lease) if max(until, lease) > now else None,
                "retry_time_is_estimate": bool(until > now),
            }
        return output
