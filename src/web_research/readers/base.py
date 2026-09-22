from __future__ import annotations

from typing import Protocol

from ..models import Document


class Reader(Protocol):
    async def read(self, url: str) -> Document: ...

    async def close(self) -> None: ...


def cap_content(content: str, max_chars: int, *, method: str) -> tuple[str, str | None]:
    """Bound document text at reader output.

    Cap extraction before caching so live reads, cached reads, and continuation snapshots
    share the same text. This memory/storage bound is separate from the smaller per-response
    output limit: callers can page through retained text, but not the discarded tail.
    A truncation warning exposes that limitation; this is not a research completion threshold.

    Returns the text and a warning string when it was capped, so the truncation is visible in
    the document rather than inferred from a short answer.
    """
    if max_chars <= 0 or len(content) <= max_chars:
        return content, None
    return content[:max_chars], f"content_truncated:{len(content)}>{max_chars}:{method}"
