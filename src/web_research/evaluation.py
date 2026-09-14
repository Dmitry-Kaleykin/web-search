from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .citations import CitationError, validate_citations
from .evidence import EvidenceBatch, EvidenceLedger
from .models import Document, Requirement, ResearchSpec, SearchResult, SourceClass, TaskType
from .ranking import gate_candidates


@dataclass(frozen=True, slots=True)
class FixtureResult:
    name: str
    passed: bool
    accepted_claims: int
    proposed_claims: int
    coverage_score: float
    sufficient: bool
    unresolved_gaps: list[str]
    conflicts: list[str]
    candidate_accepted: int
    candidate_rejected: int
    failures: list[str]
    answer_valid: bool | None = None


def evaluate_fixture(path: Path) -> FixtureResult:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Evaluation fixture must be an object: {path}")
    spec_data = _object(payload.get("spec"), "spec")
    requirements_data = spec_data.get("requirements")
    if not isinstance(requirements_data, list):
        raise ValueError(f"Fixture spec requires a requirements array: {path}")
    spec = ResearchSpec(
        original_query=str(spec_data.get("original_query") or ""),
        task_type=TaskType(str(spec_data.get("task_type") or "exploration")),
        requirements=[
            Requirement.from_dict(_object(item, "requirement"), index)
            for index, item in enumerate(requirements_data, start=1)
        ],
        subjects=[str(item) for item in spec_data.get("subjects", [])],
        freshness=_optional_string(spec_data.get("freshness")),
        as_of_date=_optional_string(spec_data.get("as_of_date")),
    )
    ledger = EvidenceLedger(spec)
    proposed = 0
    pages = payload.get("pages")
    if not isinstance(pages, list):
        raise ValueError(f"Evaluation fixture requires a pages array: {path}")
    for page in pages:
        page_data = _object(page, "page")
        document_data = _object(page_data.get("document"), "document")
        evidence_data = _object(page_data.get("evidence"), "evidence")
        claims = evidence_data.get("claims")
        if not isinstance(claims, list):
            raise ValueError(f"Fixture evidence requires a claims array: {path}")
        proposed += len(claims)
        ledger.add_document(
            Document(**document_data),
            EvidenceBatch(
                source_class=SourceClass(str(evidence_data.get("source_class") or "unknown")),
                claims=[_object(claim, "claim") for claim in claims],
            ),
        )
    coverage = ledger.coverage()
    expected = _object(payload.get("expected", {}), "expected")
    failures: list[str] = []
    if "sufficient" in expected and coverage.sufficient is not bool(expected["sufficient"]):
        failures.append(
            f"expected sufficient={bool(expected['sufficient'])}, got {coverage.sufficient}"
        )
    if "accepted_claims" in expected and len(ledger.claims) != expected["accepted_claims"]:
        failures.append(f"expected {expected['accepted_claims']} claims, got {len(ledger.claims)}")
    if "source_count" in expected and coverage.items[0].source_count != expected["source_count"]:
        failures.append(f"unexpected independent source count: {coverage.items[0].source_count}")
    answer_valid = None
    if "answer_markdown" in payload:
        valid = True
        try:
            validate_citations(
                str(payload["answer_markdown"]), ledger.evidence_sources(), ledger.claims
            )
        except CitationError:
            valid = False
        answer_valid = valid
        if valid != expected.get("answer_valid", True):
            failures.append(
                f"expected answer_valid={expected.get('answer_valid', True)}, got {valid}"
            )
    expected_gaps = sorted(str(item) for item in expected.get("unresolved_gaps", []))
    if "unresolved_gaps" in expected and sorted(coverage.unresolved_gaps) != expected_gaps:
        failures.append(
            f"expected unresolved_gaps={expected_gaps}, got {sorted(coverage.unresolved_gaps)}"
        )
    minimum_conflicts = int(expected.get("minimum_conflicts", 0))
    if len(coverage.conflicts) < minimum_conflicts:
        failures.append(
            f"expected at least {minimum_conflicts} conflict(s), got {len(coverage.conflicts)}"
        )
    minimum_accepted = int(expected.get("minimum_accepted_claims", 0))
    if len(ledger.claims) < minimum_accepted:
        failures.append(
            f"expected at least {minimum_accepted} accepted claim(s), got {len(ledger.claims)}"
        )
    candidate_accepted = 0
    candidate_rejected = 0
    candidate_gate = payload.get("candidate_gate")
    if candidate_gate is not None:
        gate_data = _object(candidate_gate, "candidate_gate")
        raw_candidates = gate_data.get("candidates")
        if not isinstance(raw_candidates, list):
            raise ValueError(f"Fixture candidate_gate requires a candidates array: {path}")
        candidates = [SearchResult(**_object(item, "candidate")) for item in raw_candidates]
        semantic_scores = _object(gate_data.get("semantic_scores", {}), "semantic_scores")
        decision = gate_candidates(
            candidates,
            search_query=str(gate_data.get("search_query") or spec.original_query),
            spec=spec,
            uncovered_requirement_ids=[item.id for item in spec.requirements],
            semantic_scores={str(url): float(score) for url, score in semantic_scores.items()},
            semantic_min_score=float(gate_data.get("semantic_min_score", 0.08)),
            semantic_relative_ratio=float(gate_data.get("semantic_relative_ratio", 0.15)),
            lexical_min_score=float(gate_data.get("lexical_min_score", 0.01)),
            rejected_batch_streak=int(gate_data.get("rejected_batch_streak", 0)),
        )
        accepted_urls = sorted(item.url for item in decision.accepted)
        rejected_urls = sorted(item.url for item, _ in decision.rejected)
        candidate_accepted = len(accepted_urls)
        candidate_rejected = len(rejected_urls)
        expected_accepted_urls = sorted(
            str(item) for item in expected.get("accepted_candidate_urls", [])
        )
        expected_rejected_urls = sorted(
            str(item) for item in expected.get("rejected_candidate_urls", [])
        )
        if accepted_urls != expected_accepted_urls:
            failures.append(
                f"expected accepted_candidate_urls={expected_accepted_urls}, got {accepted_urls}"
            )
        if rejected_urls != expected_rejected_urls:
            failures.append(
                f"expected rejected_candidate_urls={expected_rejected_urls}, got {rejected_urls}"
            )
    return FixtureResult(
        name=str(payload.get("name") or path.stem),
        passed=not failures,
        accepted_claims=len(ledger.claims),
        proposed_claims=proposed,
        coverage_score=coverage.score,
        sufficient=coverage.sufficient,
        unresolved_gaps=coverage.unresolved_gaps,
        conflicts=coverage.conflicts,
        candidate_accepted=candidate_accepted,
        candidate_rejected=candidate_rejected,
        failures=failures,
        answer_valid=answer_valid,
    )


def evaluate_directory(path: Path) -> list[FixtureResult]:
    fixtures = sorted(path.glob("*.json"))
    if not fixtures:
        raise ValueError(f"No JSON evaluation fixtures found in {path}")
    return [evaluate_fixture(fixture) for fixture in fixtures]


def evaluation_main() -> None:
    parser = argparse.ArgumentParser(
        description="Replay deterministic web-search evidence fixtures"
    )
    parser.add_argument("path", nargs="?", default="eval/fixtures", type=Path)
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument(
        "--integration",
        action="store_true",
        help="Also run local reader, OCR and Chromium evaluations "
        "(requires dev/browser dependencies)",
    )
    args = parser.parse_args()
    results = evaluate_directory(args.path)
    if args.as_json:
        print(json.dumps([asdict(result) for result in results], indent=2))
    else:
        for result in results:
            status = "PASS" if result.passed else "FAIL"
            print(
                f"{status} {result.name}: coverage={result.coverage_score:.0%} "
                f"claims={result.accepted_claims}/{result.proposed_claims} "
                f"conflicts={len(result.conflicts)} "
                f"candidates={result.candidate_accepted}/{result.candidate_rejected}"
            )
            for failure in result.failures:
                print(f"  - {failure}")
    if args.integration:
        project = Path(__file__).resolve().parents[2]
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "tests/test_reliability.py",
                "tests/test_documents.py",
                "tests/test_browser_integration.py",
            ],
            cwd=project,
            env={**os.environ, "WEB_SEARCH_RUN_BROWSER_TESTS": "1"},
            check=False,
        )
        if completed.returncode:
            raise SystemExit(completed.returncode)
    if not all(result.passed for result in results):
        raise SystemExit(1)


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"Fixture {label} must be an object")
    return value


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


if __name__ == "__main__":
    evaluation_main()
