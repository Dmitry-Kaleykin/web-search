from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from .freshness import publication_window
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
from .support import claim_supported
from .text import best_excerpt, compact_text


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
        self._fingerprints: list[tuple[set[str], str]] = []
        self._attributions: dict[str, str] = {}
        self.window = publication_window(
            spec.freshness or spec.original_query,
            as_of=date.fromisoformat(spec.as_of_date) if spec.as_of_date else None,
        )
        if self.window is None and any(r.freshness_required for r in spec.requirements):
            self.window = publication_window(
                "recent", as_of=date.fromisoformat(spec.as_of_date) if spec.as_of_date else None
            )

    def add_document(self, document: Document, batch: EvidenceBatch) -> tuple[Source, int]:
        existing = next((item for item in self.sources if item.url == document.final_url), None)
        if existing:
            return existing, 0
        words = re.findall(r"\w+", document.content.casefold())
        fingerprint = {" ".join(words[i : i + 5]) for i in range(max(1, len(words) - 4))}
        if len(words) < 40:
            fingerprint = {"short:" + hashlib.sha256(" ".join(words).encode()).hexdigest()}
        family = registrable_domain(document.final_url)
        for other, other_family in self._fingerprints:
            overlap = len(fingerprint & other) / max(1, min(len(fingerprint), len(other)))
            if overlap >= 0.85:
                family = other_family
                break
        attribution = (document.attribution or "").strip().casefold()
        if attribution:
            family = self._attributions.setdefault(attribution, family)
        self._fingerprints.append((fingerprint, family))
        source = Source(
            id=f"S{len(self.sources) + 1}",
            url=document.final_url,
            title=document.title,
            domain=registrable_domain(document.final_url),
            source_class=batch.source_class,
            source_family=family,
            retrieved_at=document.retrieved_at,
            published_at=document.published_at,
            published_at_source=document.published_at_source,
            extraction_method=document.method,
            warnings=list(document.warnings),
        )
        self.sources.append(source)
        self._source_urls.add(source.url)

        requirements = {item.id: item for item in self.spec.requirements}
        valid_requirement_ids = set(requirements)
        added = 0
        for raw in batch.claims:
            requirement_id = str(raw.get("requirement_id", ""))
            statement = compact_text(str(raw.get("statement", "")))
            excerpt = compact_text(str(raw.get("excerpt", "")))
            if requirement_id not in valid_requirement_ids or not statement:
                continue
            if requirements[requirement_id].freshness_required and (
                self.window is None or not self.window.contains(document.published_at)
            ):
                source.warnings.append(f"outside_requested_publication_window:{requirement_id}")
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
                    if self.window
                    and self.window.contains(sources_by_id[claim.source_id].published_at)
                ]
            source_ids = {claim.source_id for claim in requirement_claims}
            source_domains = {sources_by_id[source_id].source_family for source_id in source_ids}
            enough_sources = len(source_domains) >= requirement.min_sources
            if requirement.id in conflicts_by_requirement:
                reason = "materially conflicting evidence requires resolution"
                covered = False
            elif not enough_sources and requirement.freshness_required:
                reason = (
                    f"needs {requirement.min_sources} independent source(s) "
                    f"within the requested date window; has {len(source_domains)}"
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
    return claim_supported(statement, excerpt)


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
