from __future__ import annotations

from typing import Protocol

from ..models import Document


class Reader(Protocol):
    async def read(self, url: str) -> Document: ...

    async def close(self) -> None: ...


def cap_content(content: str, max_chars: int, *, method: str) -> tuple[str, str | None]:
    """Bound document text at reader output.

    This runs in the reader rather than at the cache boundary on purpose. The evidence ledger
    verifies model excerpts verbatim against ``document.content``, so the cached copy and the
    live copy must be byte-identical; truncating only one of them makes citation validity depend
    on whether the page happened to be cached.

    ``max_chars`` therefore has to stay well above what any model call actually receives
    (``_select_relevant_content`` caps at 18k), or long documents would silently stop producing
    verifiable citations. It exists to stop pathological extractions -- a single rendered page
    was measured at 16.3 MB against a 60k tool output limit.

    Returns the text and a warning string when it was capped, so the truncation is visible in
    the document rather than inferred from a short answer.
    """
    if max_chars <= 0 or len(content) <= max_chars:
        return content, None
    return content[:max_chars], f"content_truncated:{len(content)}>{max_chars}:{method}"
