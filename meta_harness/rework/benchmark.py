"""Measure the matcher, so its thresholds are chosen rather than guessed.

Matching is the part of this flow that can be wrong quietly. A bad reformat is
visible in the preview; a name matched to the wrong issue reworks a ticket
nobody meant to touch, and the only evidence is a sub-issue in the wrong place.

The matcher is deterministic, so it can be scored offline against names someone
has already resolved by hand. Runs are written in the layout the outer loop
already reads — `archive_reader`, `comparison` and `frontier` consume these
untouched — so two threshold settings can be compared with the existing
`compare-runs`, rather than by re-reading a diff and forming an impression.

Two failures are counted separately on purpose:

    a miss           — a name that should have matched did not
    a false match    — a name matched to the wrong issue

They are not equally bad. A miss leaves a row unticked and a human notices; a
false match is acted on. A threshold change that trades three misses for one
false match is a regression, and a single pass rate would call it an improvement.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from meta_harness.comparability import build_task_selection_metadata
from meta_harness.rework.matching import (
    DEFAULT_THRESHOLDS,
    STATUS_MATCHED,
    Thresholds,
    parse_line,
    resolve_names,
)

SCHEMA_VERSION = 2
BENCHMARK_NAME = "rework-name-matching"

# Expected identifier for a name that must match nothing at all.
EXPECT_NONE = ""


class BenchmarkError(ValueError):
    """Raised when a case set is malformed."""


@dataclass(frozen=True)
class MatchCase:
    """One pasted line and the issue a human says it means."""

    # The line exactly as pasted, decoration included — parsing is part of what
    # is being measured, so a pre-cleaned name would test the wrong thing.
    line: str
    expected_identifier: str = EXPECT_NONE
    note: str = ""

    @property
    def expects_a_match(self) -> bool:
        return bool(self.expected_identifier.strip())

    def task_name(self) -> str:
        parsed = parse_line(self.line)
        title = (parsed.title if parsed else "") or self.line.strip()
        # Commas would split this name inside the task-selection hash.
        return title.replace(",", " ")[:80].strip() or "unnamed"


@dataclass
class CaseSet:
    """Hand-resolved names, plus the issues they were resolved against."""

    name: str
    cases: List[MatchCase] = field(default_factory=list)
    issues: List[Dict[str, Any]] = field(default_factory=list)

    def validate(self) -> None:
        if not self.cases:
            raise BenchmarkError("a case set with no cases measures nothing")
        if not self.issues:
            raise BenchmarkError("a case set needs the issues to match against")

        known = {str(issue.get("identifier") or "") for issue in self.issues}
        seen = set()
        for case in self.cases:
            task_name = case.task_name()
            if task_name in seen:
                raise BenchmarkError(f"two cases reduce to the same name: {task_name}")
            seen.add(task_name)
            if case.expects_a_match and case.expected_identifier not in known:
                # Otherwise the case is unpassable and looks like a matcher bug.
                raise BenchmarkError(
                    f"{task_name}: expected {case.expected_identifier}, which is not in the issue list"
                )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "cases": [asdict(case) for case in self.cases],
            "issues": self.issues,
        }

    def save(self, path: Path) -> Path:
        self.validate()
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path) -> "CaseSet":
        path = Path(path)
        if not path.is_file():
            raise BenchmarkError(f"No case set at {path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise BenchmarkError(f"{path} is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise BenchmarkError(f"{path} must contain a JSON object")

        raw_cases = payload.get("cases")
        if not isinstance(raw_cases, list):
            raise BenchmarkError("'cases' must be a list")
        cases = []
        for index, raw in enumerate(raw_cases):
            if not isinstance(raw, dict):
                raise BenchmarkError(f"case {index} is not an object")
            unknown = set(raw) - {"line", "expected_identifier", "note"}
            if unknown:
                raise BenchmarkError(f"case {index}: unknown fields {sorted(unknown)}")
            cases.append(
                MatchCase(
                    line=str(raw.get("line", "")),
                    expected_identifier=str(raw.get("expected_identifier", "")),
                    note=str(raw.get("note", "")),
                )
            )

        case_set = cls(
            name=str(payload.get("name", path.stem)),
            cases=cases,
            issues=list(payload.get("issues") or []),
        )
        case_set.validate()
        return case_set


# The three outcomes worth telling apart.
OUTCOME_CORRECT = "correct"
OUTCOME_MISS = "miss"
OUTCOME_FALSE_MATCH = "false_match"


@dataclass
class CaseOutcome:
    task_name: str
    expected: str
    actual: str
    status: str
    outcome: str
    method: str = ""

    @property
    def passed(self) -> bool:
        return self.outcome == OUTCOME_CORRECT

    def to_record(self) -> Dict[str, Any]:
        return {
            "task_name": self.task_name,
            "passed": self.passed,
            "reward": 1.0 if self.passed else 0.0,
            "expected_identifier": self.expected,
            "actual_identifier": self.actual,
            "resolution_status": self.status,
            "outcome": self.outcome,
            "method": self.method,
        }


@dataclass
class BenchmarkResult:
    case_set_name: str
    thresholds: Thresholds
    run_dir: Path
    outcomes: List[CaseOutcome] = field(default_factory=list)

    @property
    def pass_rate(self) -> float:
        if not self.outcomes:
            return 0.0
        return sum(1 for outcome in self.outcomes if outcome.passed) / len(self.outcomes)

    @property
    def misses(self) -> List[CaseOutcome]:
        return [o for o in self.outcomes if o.outcome == OUTCOME_MISS]

    @property
    def false_matches(self) -> List[CaseOutcome]:
        return [o for o in self.outcomes if o.outcome == OUTCOME_FALSE_MATCH]

    def summary_line(self) -> str:
        passed = sum(1 for outcome in self.outcomes if outcome.passed)
        parts = [f"{passed}/{len(self.outcomes)} correct ({self.pass_rate:.0%})"]
        if self.false_matches:
            parts.append(f"{len(self.false_matches)} FALSE MATCH")
        if self.misses:
            parts.append(f"{len(self.misses)} missed")
        return ", ".join(parts)


def _classify(case: MatchCase, resolution) -> CaseOutcome:
    matched = resolution.status == STATUS_MATCHED and resolution.chosen
    actual = resolution.chosen.identifier if matched else ""

    if case.expects_a_match:
        if actual == case.expected_identifier:
            outcome = OUTCOME_CORRECT
        elif actual:
            outcome = OUTCOME_FALSE_MATCH  # matched, but to the wrong issue
        else:
            outcome = OUTCOME_MISS
    else:
        # The case says nothing should match. Matching anything is a false match.
        outcome = OUTCOME_CORRECT if not actual else OUTCOME_FALSE_MATCH

    return CaseOutcome(
        task_name=case.task_name(),
        expected=case.expected_identifier,
        actual=actual,
        status=resolution.status,
        outcome=outcome,
        method=resolution.method,
    )


def run_benchmark(
    case_set: CaseSet,
    *,
    archive_root: Path,
    thresholds: Thresholds = DEFAULT_THRESHOLDS,
    candidate_name: str = "default-thresholds",
    run_name: Optional[str] = None,
) -> BenchmarkResult:
    """Score the matcher over a case set and write a run directory.

    Deliberately runs without a reasoner: the model is not deterministic, and a
    benchmark that moves on its own cannot attribute a change to a threshold.
    """
    case_set.validate()
    thresholds.validate()

    started_at = datetime.now(timezone.utc)
    parsed = [parse_line(case.line) for case in case_set.cases]
    resolutions = resolve_names(
        [name for name in parsed if name is not None], case_set.issues, thresholds=thresholds
    )

    outcomes: List[CaseOutcome] = []
    cursor = 0
    for case, name in zip(case_set.cases, parsed):
        if name is None:
            # A line that parses to nothing matched nothing, by definition.
            outcomes.append(
                CaseOutcome(
                    task_name=case.task_name(),
                    expected=case.expected_identifier,
                    actual="",
                    status="unparsed",
                    outcome=OUTCOME_CORRECT if not case.expects_a_match else OUTCOME_MISS,
                )
            )
            continue
        outcomes.append(_classify(case, resolutions[cursor]))
        cursor += 1

    slug = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in candidate_name)
    run_dir = Path(archive_root).expanduser() / (
        run_name or f"{slug}-{started_at.strftime('%Y%m%dT%H%M%SZ')}"
    )
    result = BenchmarkResult(
        case_set_name=case_set.name,
        thresholds=thresholds,
        run_dir=run_dir,
        outcomes=outcomes,
    )
    _write_run_dir(result, case_set, candidate_name, started_at)
    return result


def _write_run_dir(
    result: BenchmarkResult,
    case_set: CaseSet,
    candidate_name: str,
    started_at: datetime,
) -> None:
    run_dir = result.run_dir
    (run_dir / "tasks").mkdir(parents=True, exist_ok=True)

    records = [outcome.to_record() for outcome in result.outcomes]
    for record in records:
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in record["task_name"])
        (run_dir / "tasks" / f"{safe}.json").write_text(
            json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    summary = {
        "benchmark_name": BENCHMARK_NAME,
        "candidate_name": candidate_name,
        "candidate_path": json.dumps(result.thresholds.describe(), sort_keys=True),
        "eval_metrics": {
            "eval/pass_rate": result.pass_rate,
            "eval/total_tasks": len(result.outcomes),
            "eval/passed_tasks": sum(1 for outcome in result.outcomes if outcome.passed),
            # Tracked apart from pass rate because trading misses for false
            # matches must never read as an improvement.
            "eval/false_matches": len(result.false_matches),
            "eval/misses": len(result.misses),
        },
        "task_results": records,
        "case_set": case_set.name,
    }
    (run_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    manifest = {
        "benchmark_name": BENCHMARK_NAME,
        "candidate_name": candidate_name,
        "created_at": started_at.isoformat(),
        "outer_loop": {
            "benchmark_runner": {
                "schema_version": SCHEMA_VERSION,
                "benchmark": BENCHMARK_NAME,
                "candidate": candidate_name,
                "total_tasks": len(result.outcomes),
                "task_selection": build_task_selection_metadata(
                    task_filter=",".join(outcome.task_name for outcome in result.outcomes),
                    skip_tasks=None,
                ),
            },
            "provenance": {
                "case_set": case_set.name,
                "issue_count": len(case_set.issues),
                "thresholds": result.thresholds.describe(),
            },
        },
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def sweep(
    case_set: CaseSet,
    *,
    archive_root: Path,
    candidates: Sequence[tuple],
) -> List[BenchmarkResult]:
    """Score several threshold settings over the same cases.

    `candidates` is a sequence of (name, Thresholds). Every run lands in the
    same archive, so `show-frontier` ranks them and `compare-runs` diffs any
    two — the task selection hash matches because the cases are identical.
    """
    return [
        run_benchmark(
            case_set,
            archive_root=archive_root,
            thresholds=thresholds,
            candidate_name=name,
            run_name=name,
        )
        for name, thresholds in candidates
    ]
