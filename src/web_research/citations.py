from __future__ import annotations

import re

from .models import Source

CITATION_RE = re.compile(r"\[(S\d+)\]")


class CitationError(ValueError):
    pass


def validate_citations(answer: str, sources: list[Source]) -> None:
    known = {item.id for item in sources}
    cited = set(CITATION_RE.findall(answer))
    unknown = sorted(cited - known)
    if unknown:
        raise CitationError(f"Answer cited unknown sources: {', '.join(unknown)}")
    if sources and not cited:
        raise CitationError("Answer contains no source citations")


def append_sources(answer: str, sources: list[Source]) -> str:
    if not sources:
        return answer.rstrip()
    lines = [answer.rstrip(), "", "### Sources", ""]
    for source in sources:
        date = f" ({source.published_at})" if source.published_at else ""
        lines.append(f"- [{source.id}] [{source.title}]({source.url}){date}")
    return "\n".join(lines)
