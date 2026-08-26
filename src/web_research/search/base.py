from __future__ import annotations

from typing import Protocol

from ..models import SearchResult


class SearchProvider(Protocol):
    async def search(
        self,
        query: str,
        *,
        page: int = 1,
        language: str | None = None,
        time_range: str | None = None,
        categories: str | None = None,
        limit: int = 10,
    ) -> list[SearchResult]: ...

    async def close(self) -> None: ...
