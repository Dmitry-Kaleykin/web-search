from __future__ import annotations

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
            if not excerpt or excerpt.lower() not in compact_text(document.content).lower():
                requirement = next(
                    item for item in self.spec.requirements if item.id == requirement_id
                )
                excerpt = best_excerpt(document.content, requirement.question)
                source.warnings.append(f"non_verbatim_excerpt_replaced:{requirement_id}")
            try:
                confidence = float(raw.get("confidence", 0.5))
            except (TypeError, ValueError):
                confidence = 0.5
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

        items: list[CoverageItem] = []
        total_weight = 0.0
        covered_weight = 0.0
        unresolved: list[str] = []
        for requirement in self.spec.requirements:
            requirement_claims = claims_by_requirement[requirement.id]
            source_ids = {claim.source_id for claim in requirement_claims}
            source_domains = {sources_by_id[source_id].domain for source_id in source_ids}
            has_primary = any(
                sources_by_id[source_id].source_class == SourceClass.PRIMARY
                for source_id in source_ids
            )
            enough_sources = len(source_domains) >= requirement.min_sources
            covered = enough_sources and (has_primary or not requirement.primary_required)
            if not enough_sources:
                reason = f"needs {requirement.min_sources} source(s); has {len(source_domains)}"
            elif requirement.primary_required and not has_primary:
                reason = "needs a primary source"
            else:
                reason = "evidence rule satisfied"
            item = CoverageItem(
                requirement_id=requirement.id,
                covered=covered,
                source_count=len(source_domains),
                has_primary=has_primary,
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
            conflicts=list(self.conflicts),
        )

    def evidence_summary(self, *, max_chars: int = 30_000) -> str:
        source_by_id = {item.id: item for item in self.sources}
        lines: list[str] = []
        size = 0
        for claim in self.claims:
            source = source_by_id[claim.source_id]
            line = (
                f"{claim.id} requirement={claim.requirement_id} source={claim.source_id} "
                f"class={source.source_class} stance={claim.stance}\n"
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


def _importance_weight(importance: Importance) -> float:
    return {Importance.REQUIRED: 3.0, Importance.IMPORTANT: 1.5, Importance.OPTIONAL: 0.5}[
        importance
    ]
