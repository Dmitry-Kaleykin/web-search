from __future__ import annotations

import re
from datetime import date
from typing import Any

from .citations import append_sources, validate_citations
from .evidence import EvidenceBatch, EvidenceLedger
from .model.base import ResearchModel
from .models import (
    Document,
    Importance,
    Requirement,
    ResearchSpec,
    SourceClass,
    TaskType,
)
from .prompts import (
    ANSWER_SCHEMA,
    ANSWER_SYSTEM,
    ASSESS_SCHEMA,
    ASSESS_SYSTEM,
    EVIDENCE_SCHEMA,
    EVIDENCE_SYSTEM,
    QUERY_SCHEMA,
    QUERY_SYSTEM,
    SPEC_SCHEMA,
    SPEC_SYSTEM,
    answer_user,
    assess_user,
    evidence_user,
    query_user,
    spec_user,
)
from .text import best_excerpt, lexical_similarity


class ResearchAgent:
    def __init__(
        self,
        model: ResearchModel,
        *,
        evidence_model: ResearchModel | None = None,
        current_date: str | None = None,
    ) -> None:
        self.model = model
        self.evidence_model = evidence_model or model
        self.current_date = current_date or date.today().isoformat()

    async def compile_spec(self, query: str, freshness: str | None) -> ResearchSpec:
        data = await self.model.complete_json(
            system=SPEC_SYSTEM,
            user=spec_user(query, freshness, self.current_date),
            schema_name="research_spec",
            schema=SPEC_SCHEMA,
        )
        try:
            task_type = TaskType(str(data.get("task_type", "exploration")))
        except ValueError:
            task_type = TaskType.EXPLORATION
        raw_requirements = data.get("requirements")
        requirements = []
        if isinstance(raw_requirements, list):
            requirements = [
                Requirement.from_dict(item, index)
                for index, item in enumerate(raw_requirements, start=1)
                if isinstance(item, dict)
            ]
        requirements = [item for item in requirements if item.question][:30]
        if not requirements:
            requirements = heuristic_spec(query, freshness).requirements
        subjects = data.get("subjects")
        return ResearchSpec(
            original_query=query,
            task_type=task_type,
            requirements=requirements,
            subjects=[str(item) for item in subjects if str(item).strip()]
            if isinstance(subjects, list)
            else [],
            answer_format=str(data.get("answer_format") or "A cited, decision-useful answer."),
            locale=_optional_string(data.get("locale")),
            freshness=freshness,
        )

    async def plan_queries(self, spec: ResearchSpec, gaps: list[str] | None = None) -> list[str]:
        data = await self.model.complete_json(
            system=QUERY_SYSTEM,
            user=query_user(spec, gaps, self.current_date),
            schema_name="search_queries",
            schema=QUERY_SCHEMA,
        )
        raw = data.get("queries")
        if not isinstance(raw, list):
            return []
        return _unique_queries([str(item) for item in raw])[:8]

    async def analyze_document(self, spec: ResearchSpec, document: Document) -> EvidenceBatch:
        content = _select_relevant_content(spec, document.content)
        data = await self.evidence_model.complete_json(
            system=EVIDENCE_SYSTEM,
            user=evidence_user(
                spec, document.final_url, document.title, content, self.current_date
            ),
            schema_name="source_evidence",
            schema=EVIDENCE_SCHEMA,
        )
        try:
            source_class = SourceClass(str(data.get("source_class", "unknown")))
        except ValueError:
            source_class = SourceClass.UNKNOWN
        claims = data.get("claims")
        return EvidenceBatch(
            source_class=source_class,
            claims=[item for item in claims if isinstance(item, dict)]
            if isinstance(claims, list)
            else [],
        )

    async def assess(self, spec: ResearchSpec, ledger: EvidenceLedger) -> dict[str, Any]:
        coverage = ledger.coverage()
        return await self.model.complete_json(
            system=ASSESS_SYSTEM,
            user=assess_user(
                spec,
                coverage,
                ledger.evidence_summary(max_chars=12_000),
                self.current_date,
            ),
            schema_name="sufficiency_assessment",
            schema=ASSESS_SCHEMA,
        )

    async def synthesize(self, spec: ResearchSpec, ledger: EvidenceLedger) -> str:
        coverage = ledger.coverage()
        sources = ledger.evidence_sources()
        data = await self.model.complete_json(
            system=ANSWER_SYSTEM,
            user=answer_user(
                spec,
                coverage,
                ledger.evidence_summary(),
                sources,
                self.current_date,
            ),
            schema_name="research_answer",
            schema=ANSWER_SCHEMA,
        )
        answer = str(data.get("answer_markdown") or "").strip()
        if not answer:
            raise ValueError("The model returned an empty answer")
        validate_citations(answer, sources)
        return append_sources(answer, sources)


def heuristic_spec(query: str, freshness: str | None) -> ResearchSpec:
    lower = query.lower()
    if any(token in lower for token in ("compare", "comparison", " versus ", " vs ")):
        task_type = TaskType.COMPARISON
    elif any(token in lower for token in ("recommend", "best ", "which should")):
        task_type = TaskType.RECOMMENDATION
    elif freshness or any(token in lower for token in ("today", "latest", "current", "recent")):
        task_type = TaskType.CURRENT_EVENT
    elif lower.startswith(("what is", "who is", "when did", "where is")):
        task_type = TaskType.FACT
    elif lower.startswith(("why", "how")):
        task_type = TaskType.EXPLANATION
    else:
        task_type = TaskType.EXPLORATION
    min_sources = (
        2
        if task_type
        in {
            TaskType.COMPARISON,
            TaskType.RECOMMENDATION,
            TaskType.CURRENT_EVENT,
        }
        else 1
    )
    requirement = Requirement(
        id="R1",
        question=query,
        importance=Importance.REQUIRED,
        min_sources=min_sources,
    )
    return ResearchSpec(
        original_query=query,
        task_type=task_type,
        requirements=[requirement],
        freshness=freshness,
    )


def heuristic_evidence(spec: ResearchSpec, document: Document) -> EvidenceBatch:
    claims: list[dict[str, Any]] = []
    for requirement in spec.requirements:
        similarity = lexical_similarity(document.content[:20_000], requirement.question)
        if similarity < 0.015:
            continue
        excerpt = best_excerpt(document.content, requirement.question)
        if not excerpt:
            continue
        claims.append(
            {
                "requirement_id": requirement.id,
                "statement": excerpt,
                "excerpt": excerpt,
                "confidence": min(0.65, 0.35 + similarity * 2),
                "stance": "supports",
            }
        )
    return EvidenceBatch(source_class=SourceClass.UNKNOWN, claims=claims)


def fallback_answer(spec: ResearchSpec, ledger: EvidenceLedger) -> str:
    coverage = ledger.coverage()
    source_by_id = {item.id: item for item in ledger.evidence_sources()}
    lines = ["I gathered the following evidence:", ""]
    if not ledger.claims:
        lines = [
            "I could not gather enough usable evidence to answer the request reliably.",
            "",
        ]
    else:
        for claim in ledger.claims[:20]:
            lines.append(f"- {claim.statement} [{claim.source_id}]")
    if coverage.unresolved_gaps:
        descriptions = {
            item.id: item.question
            for item in spec.requirements
            if item.id in coverage.unresolved_gaps
        }
        lines.extend(
            [
                "",
                "Unresolved evidence gaps:",
                *[f"- {gap}: {descriptions.get(gap, gap)}" for gap in coverage.unresolved_gaps],
            ]
        )
    return append_sources("\n".join(lines), list(source_by_id.values()))


def _select_relevant_content(spec: ResearchSpec, content: str, limit: int = 18_000) -> str:
    paragraphs = [item.strip() for item in re.split(r"\n\s*\n", content) if item.strip()]
    targets = [item.question for item in spec.requirements]
    scored = sorted(
        enumerate(paragraphs),
        key=lambda pair: max(
            (lexical_similarity(pair[1], target) for target in targets), default=0.0
        ),
        reverse=True,
    )
    selected_indexes = {index for index, _ in scored[:24]}
    selected_indexes.update(range(min(4, len(paragraphs))))
    output: list[str] = []
    size = 0
    for index in sorted(selected_indexes):
        paragraph = paragraphs[index]
        if size + len(paragraph) > limit:
            continue
        output.append(paragraph)
        size += len(paragraph)
    return "\n\n".join(output) or content[:limit]


def _unique_queries(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = " ".join(value.split()).strip()
        key = normalized.casefold()
        if normalized and key not in seen:
            result.append(normalized)
            seen.add(key)
    return result


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
