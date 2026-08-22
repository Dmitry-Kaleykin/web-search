from __future__ import annotations

from typing import Protocol

from ..models import Document


class Reader(Protocol):
    async def read(self, url: str) -> Document: ...

    async def close(self) -> None: ...
