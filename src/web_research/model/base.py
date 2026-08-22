from __future__ import annotations

from typing import Any, Protocol


class ResearchModel(Protocol):
    async def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema_name: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]: ...

    async def close(self) -> None: ...
