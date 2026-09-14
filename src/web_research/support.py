"""Conservative text support gates; these do not claim general semantic entailment."""

from __future__ import annotations

import re

from .text import compact_text, lexical_similarity

NUMERIC_FACT_RE = re.compile(r"(?:[$€£¥])?\d+(?:[.,:/+-]\d+)*%?[A-Za-z]?")
NEGATION = re.compile(
    r"\b(?:not|no|never|without|cannot|neither|none|unsupported|unavailable|n't)\b"
    r"|n['\u2019]t\b",
    re.I,
)
QUALIFIERS = re.compile(
    r"\b(?:may|might|could|allegedly|reportedly|estimated|approximately|"
    r"up to|at least|at most|only|except|unless|claims?|says?|said|reports?|reported|"
    r"believes?|suggests?|according to)\b",
    re.I,
)


def claim_supported(statement: str, excerpt: str) -> bool:
    claim, source = compact_text(statement), compact_text(excerpt)
    if not claim or not source:
        return False
    # Compare against individual source sentences so unrelated matching words cannot hide negation.
    if claim.casefold().rstrip(".") == source.casefold().rstrip("."):
        return True
    sentences = re.split(r"(?<=[.!?])\s+|\n", source)
    best = max(sentences, key=lambda s: lexical_similarity(claim, s))
    if lexical_similarity(claim, best) < 0.35:
        return False
    if bool(NEGATION.search(claim)) != bool(NEGATION.search(best)):
        return False
    if {x.casefold() for x in NUMERIC_FACT_RE.findall(claim)} - {
        x.casefold() for x in NUMERIC_FACT_RE.findall(best)
    }:
        return False
    # Preserve uncertainty and limitations when paraphrasing.
    if {x.casefold() for x in QUALIFIERS.findall(best)} - {
        x.casefold() for x in QUALIFIERS.findall(claim)
    }:
        return False

    # New content words often introduce a new entity, predicate or attribution.
    def words(value: str) -> set[str]:
        stop = {
            "a",
            "an",
            "the",
            "is",
            "are",
            "was",
            "were",
            "be",
            "been",
            "has",
            "have",
            "had",
            "of",
            "to",
            "in",
            "on",
            "and",
            "it",
            "its",
            "does",
            "do",
            "did",
            "according",
        }
        return {w.rstrip("s") for w in re.findall(r"\b\w+\b", value.casefold()) if w not in stop}

    if not words(claim) <= words(best):
        return False
    # Preserve content-word order to reject swapped actors/values (A beat B vs B beat A).
    wanted = [
        w.rstrip("s")
        for w in re.findall(r"\b\w+\b", claim.casefold())
        if w.rstrip("s") in words(claim)
    ]
    available = iter(w.rstrip("s") for w in re.findall(r"\b\w+\b", best.casefold()))
    return all(any(word == candidate for candidate in available) for word in wanted)
