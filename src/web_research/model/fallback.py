from __future__ import annotations

import logging
from typing import Any

from .base import ResearchModel

LOGGER = logging.getLogger(__name__)


class FallbackModelClient:
    """Use a dedicated model when healthy and fall back to another model on failure."""

    def __init__(
        self,
        preferred: ResearchModel,
        fallback: ResearchModel,
        *,
        disable_after_failures: int = 2,
    ) -> None:
        self.preferred = preferred
        self.fallback = fallback
        self.disable_after_failures = max(1, disable_after_failures)
        self.failures = 0
        self.disabled = False

    async def close(self) -> None:
        await self.preferred.close()

    async def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema_name: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        if not self.disabled:
            try:
                return await self.preferred.complete_json(
                    system=system,
                    user=user,
                    schema_name=schema_name,
                    schema=schema,
                )
            except Exception as exc:
                self.failures += 1
                if self.failures >= self.disable_after_failures:
                    self.disabled = True
                LOGGER.warning(
                    "Dedicated evidence model failed for %s (%d/%d); using the dynamic model: %s",
                    schema_name,
                    self.failures,
                    self.disable_after_failures,
                    exc,
                )
        return await self.fallback.complete_json(
            system=system,
            user=user,
            schema_name=schema_name,
            schema=schema,
        )
