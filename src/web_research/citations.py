from __future__ import annotations

import re

from .models import Claim, Source
from .support import claim_supported

CITATION_RE = re.compile(r"\[(S\d+)\]")


class CitationError(ValueError):
    pass


def validate_citations(
    answer: str, sources: list[Source], claims: list[Claim] | None = None
) -> None:
    known = {item.id for item in sources}
    cited = set(CITATION_RE.findall(answer))
    unknown = sorted(cited - known)
    if unknown:
        raise CitationError(f"Answer cited unknown sources: {', '.join(unknown)}")
    if sources and not cited:
        raise CitationError("Answer contains no source citations")
    if claims is not None:
        validate_answer_support(answer, claims)


def validate_answer_support(answer: str, claims: list[Claim]) -> None:
    """Check every answer sentence against excerpts belonging to its actual citations.

    Deliberately conservative: unverified synthesis falls back to verbatim evidence in the caller.
    Topic headings are allowed; factual prose and table rows require local citations.
    """
    for line in answer.splitlines():
        line = line.strip()
        if not line or (line.startswith("#") and not CITATION_RE.search(line)):
            continue
        if re.fullmatch(r"[| :\-]+", line):
            continue
        position = 0
        matches = list(re.finditer(r"(?:\[S\d+\]\s*)+", line))
        if not matches:
            raise CitationError(f"Uncited answer text: {line[:120]}")
        for match in matches:
            text = line[position : match.start()].strip(" -*|\t")
            text = re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", text)
            text = text.replace("**", "").replace("`", "")
            ids = set(CITATION_RE.findall(match.group()))
            excerpts = [claim.excerpt for claim in claims if claim.source_id in ids]
            sentences = [part.strip(" .;|:") for part in re.split(r"(?<=[.!?])\s+|\s*\|\s*", text)]
            for sentence in filter(None, sentences):
                if not any(claim_supported(sentence, excerpt) for excerpt in excerpts):
                    raise CitationError(f"Citation does not support answer text: {sentence[:120]}")
            position = match.end()
        if line[position:].strip(" .;|\t"):
            raise CitationError("Answer ends with uncited text")


def append_sources(answer: str, sources: list[Source]) -> str:
    if not sources:
        return answer.rstrip()
    lines = [answer.rstrip(), "", "### Sources", ""]
    for source in sources:
        date = f" ({source.published_at})" if source.published_at else ""
        lines.append(f"- [{source.id}] [{source.title}]({source.url}){date}")
    return "\n".join(lines)
