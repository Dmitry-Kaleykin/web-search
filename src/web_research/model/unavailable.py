from __future__ import annotations

from typing import Any

from .errors import ModelError


class UnavailableModelClient:
    """Produces a clear error that activates deterministic controller fallbacks."""

    async def close(self) -> None:
        return None

    async def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema_name: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        del system, user, schema
        raise ModelError(
            f"No model is available for {schema_name}: the MCP client does not support "
            "sampling and no direct model fallback is configured"
        )
