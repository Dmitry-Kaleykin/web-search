from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

from .models import (
    Claim,
    CoverageItem,
    CoverageReport,
    Document,
    Importance,
    ResearchSpec,
    Source,
    SourceClass,
)
from .safety.urls import registrable_domain
from .text import best_excerpt, compact_text, lexical_similarity

MIN_CLAIM_EXCERPT_SIMILARITY = 0.18
NUMERIC_FACT_RE = re.compile(r"(?:[$€£¥])?\d+(?:[.,:/+-]\d+)*%?[A-Za-z]?")


@dataclass(slots=True)
class EvidenceBatch:
    source_class: SourceClass
    claims: list[dict]


class EvidenceLedger:
    def __init__(self, spec: ResearchSpec) -> None:
        self.spec = spec
        self.sources: list[Source] = []
        self.claims: list[Claim] = []
        self.conflicts: list[str] = []
        self._source_urls: set[str] = set()

    def add_document(self, document: Document, batch: EvidenceBatch) -> tuple[Source, int]:
        existing = next((item for item in self.sources if item.url == document.final_url), None)
        if existing:
            return existing, 0
        source = Source(
            id=f"S{len(self.sources) + 1}",
            url=document.final_url,
            title=document.title,
            domain=registrable_domain(document.final_url),
            source_class=batch.source_class,
            retrieved_at=document.retrieved_at,
            published_at=document.published_at,
            published_at_source=document.published_at_source,
            extraction_method=document.method,
            warnings=list(document.warnings),
        )
        self.sources.append(source)
        self._source_urls.add(source.url)

        valid_requirement_ids = {item.id for item in self.spec.requirements}
        added = 0
        for raw in batch.claims:
            requirement_id = str(raw.get("requirement_id", ""))
            statement = compact_text(str(raw.get("statement", "")))
            excerpt = compact_text(str(raw.get("excerpt", "")))
            if requirement_id not in valid_requirement_ids or not statement:
                continue
            excerpt_replaced = False
            if not excerpt or excerpt.lower() not in compact_text(document.content).lower():
                excerpt = best_excerpt(document.content, statement)
                source.warnings.append(f"non_verbatim_excerpt_replaced:{requirement_id}")
                excerpt_replaced = True
            if not _claim_supported(statement, excerpt):
                source.warnings.append(f"unsupported_claim_rejected:{requirement_id}")
                continue
            try:
                confidence = float(raw.get("confidence", 0.5))
            except (TypeError, ValueError):
                confidence = 0.5
            if excerpt_replaced:
                confidence = min(confidence, 0.65)
            stance = str(raw.get("stance", "supports"))
            if stance not in {"supports", "refutes", "contextualizes"}:
                stance = "supports"
            claim = Claim(
                id=f"C{len(self.claims) + 1}",
                requirement_id=requirement_id,
                source_id=source.id,
                statement=statement,
                excerpt=excerpt,
                confidence=max(0.0, min(1.0, confidence)),
                stance=stance,
                value_kind=_optional_value(raw.get("value_kind")),
                normalized_value=_optional_value(raw.get("normalized_value")),
            )
            self.claims.append(claim)
            added += 1
        return source, added

    def coverage(self) -> CoverageReport:
        claims_by_requirement: dict[str, list[Claim]] = defaultdict(list)
        sources_by_id = {item.id: item for item in self.sources}
        for claim in self.claims:
            if claim.stance == "supports" and claim.confidence >= 0.45:
                claims_by_requirement[claim.requirement_id].append(claim)

        conflicts_by_requirement, conflict_messages = self._detect_conflicts(sources_by_id)
        self.conflicts = conflict_messages
        base_coverage: dict[str, tuple[bool, int, str]] = {}
        for requirement in self.spec.requirements:
            requirement_claims = claims_by_requirement[requirement.id]
            if requirement.freshness_required:
                requirement_claims = [
                    claim
                    for claim in requirement_claims
                    if sources_by_id[claim.source_id].published_at is not None
                ]
            source_ids = {claim.source_id for claim in requirement_claims}
            source_domains = {sources_by_id[source_id].domain for source_id in source_ids}
            enough_sources = len(source_domains) >= requirement.min_sources
            if requirement.id in conflicts_by_requirement:
                reason = "materially conflicting evidence requires resolution"
                covered = False
            elif not enough_sources and requirement.freshness_required:
                reason = (
                    f"needs {requirement.min_sources} dated source(s); has {len(source_domains)}"
                )
                covered = False
            elif not enough_sources:
                reason = f"needs {requirement.min_sources} source(s); has {len(source_domains)}"
                covered = False
            else:
                reason = "evidence rule satisfied"
                covered = True
            base_coverage[requirement.id] = (covered, len(source_domains), reason)

        items: list[CoverageItem] = []
        total_weight = 0.0
        covered_weight = 0.0
        unresolved: list[str] = []
        for requirement in self.spec.requirements:
            covered, source_count, reason = base_coverage[requirement.id]
            blocked = [
                dependency
                for dependency in requirement.depends_on
                if not base_coverage.get(dependency, (False, 0, ""))[0]
            ]
            if blocked:
                covered = False
                reason = f"blocked by unresolved prerequisite(s): {', '.join(blocked)}"
            item = CoverageItem(
                requirement_id=requirement.id,
                covered=covered,
                source_count=source_count,
                reason=reason,
            )
            items.append(item)
            weight = _importance_weight(requirement.importance)
            total_weight += weight
            if covered:
                covered_weight += weight
            elif requirement.importance == Importance.REQUIRED:
                unresolved.append(requirement.id)

        score = covered_weight / total_weight if total_weight else 0.0
        return CoverageReport(
            score=score,
            sufficient=not unresolved,
            items=items,
            unresolved_gaps=unresolved,
            conflicts=conflict_messages,
        )

    def _detect_conflicts(self, sources_by_id: dict[str, Source]) -> tuple[set[str], list[str]]:
        by_requirement: dict[str, list[Claim]] = defaultdict(list)
        for claim in self.claims:
            if claim.confidence >= 0.45:
                by_requirement[claim.requirement_id].append(claim)

        conflicted: set[str] = set()
        messages: list[str] = []
        for requirement_id, claims in by_requirement.items():
            supporting_domains = {
                sources_by_id[claim.source_id].domain
                for claim in claims
                if claim.stance == "supports"
            }
            refuting_domains = {
                sources_by_id[claim.source_id].domain
                for claim in claims
                if claim.stance == "refutes"
            }
            if supporting_domains and refuting_domains and supporting_domains != refuting_domains:
                conflicted.add(requirement_id)
                messages.append(
                    f"{requirement_id}: supporting and refuting evidence comes from "
                    "different domains"
                )

            values_by_kind: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
            for claim in claims:
                if (
                    claim.stance != "supports"
                    or claim.value_kind is None
                    or claim.normalized_value is None
                ):
                    continue
                value_key = _comparable_value_key(claim.normalized_value)
                values_by_kind[claim.value_kind.casefold()][value_key].add(
                    sources_by_id[claim.source_id].domain
                )
            for value_kind, value_domains in values_by_kind.items():
                if len(value_domains) < 2:
                    continue
                all_domains = set().union(*value_domains.values())
                if len(all_domains) < 2:
                    continue
                conflicted.add(requirement_id)
                values = ", ".join(sorted(value_domains))
                messages.append(
                    f"{requirement_id}: sources report conflicting {value_kind} values: {values}"
                )
        return conflicted, list(dict.fromkeys(messages))

    def evidence_summary(self, *, max_chars: int = 30_000) -> str:
        source_by_id = {item.id: item for item in self.sources}
        lines: list[str] = []
        size = 0
        for claim in self.claims:
            source = source_by_id[claim.source_id]
            line = (
                f"{claim.id} requirement={claim.requirement_id} source={claim.source_id} "
                f"class={source.source_class} stance={claim.stance} "
                f"published_at={source.published_at or 'unknown'} "
                f"value_kind={claim.value_kind or 'none'} "
                f"normalized_value={claim.normalized_value or 'none'}\n"
                f"Statement: {claim.statement}\nExcerpt: {claim.excerpt}\n"
            )
            if size + len(line) > max_chars:
                break
            lines.append(line)
            size += len(line)
        return "\n".join(lines)

    def evidence_sources(self) -> list[Source]:
        used = {claim.source_id for claim in self.claims}
        return [source for source in self.sources if source.id in used]


def _claim_supported(statement: str, excerpt: str) -> bool:
    if not excerpt or lexical_similarity(statement, excerpt) < MIN_CLAIM_EXCERPT_SIMILARITY:
        return False
    excerpt_facts = {item.casefold() for item in NUMERIC_FACT_RE.findall(excerpt)}
    statement_facts = {item.casefold() for item in NUMERIC_FACT_RE.findall(statement)}
    return statement_facts <= excerpt_facts


def _comparable_value_key(value: str) -> str:
    """Prefer conservative numeric comparison over formatting-sensitive text equality."""
    numeric_parts = re.findall(r"\d+(?:[.,:/+-]\d+)*", value.casefold())
    if numeric_parts:
        return "|".join(part.replace(",", "") for part in numeric_parts)
    return compact_text(value).casefold()


def _importance_weight(importance: Importance) -> float:
    return {Importance.REQUIRED: 3.0, Importance.IMPORTANT: 1.5, Importance.OPTIONAL: 0.5}[
        importance
    ]


def _optional_value(value: object) -> str | None:
    if value is None:
        return None
    text = compact_text(str(value))
    return text[:200] or None
