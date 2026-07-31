"""Run a candidate against a labelled set and record the result.

This replaces the Hermes coupling. `benchmark_runner.py` builds a shell command
to run hermes-agent on TBLite or TerminalBench; this runs *our* harness on *our*
tasks. Everything downstream — `archive_reader`, `comparison`, `frontier` — is
domain-agnostic and needs no changes, because it only ever consumes a run
directory.

So the contract to honour is the layout, not the code:

    <run_dir>/summary.json    benchmark_name, candidate_name, eval_metrics
    <run_dir>/manifest.json   outer_loop.benchmark_runner.task_selection
    <run_dir>/tasks/*.json    one record per task: task_name, passed, reward

Match that and the existing outer loop compares and ranks these runs exactly as
it would a Hermes benchmark.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Callable, Dict, List, Optional

from meta_harness.clickup_bridge import ClickUpReadError
from meta_harness.comparability import build_task_selection_metadata
from meta_harness.evaluation.candidates import Candidate
from meta_harness.evaluation.dataset import Dataset
from meta_harness.linear_bridge import LinearReadError
from meta_harness.module_relevance import (
    ModuleContextUnavailableError,
    ModuleRelevanceError,
    analyze_module_relevance,
    retrieve_module_context,
)

OnStep = Optional[Callable[[str], None]]

# Failures that belong to one ticket rather than to the run. A tracker outage or
# an unparseable answer says nothing about whether the candidate judges well, so
# these cost that task and no more.
TASK_ERRORS = (
    ClickUpReadError,
    LinearReadError,
    ModuleRelevanceError,
    ModuleContextUnavailableError,
    ValueError,
)

SCHEMA_VERSION = 2


def _report(on_step: OnStep, message: str) -> None:
    if on_step is not None:
        on_step(message)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _package_version() -> str:
    try:
        return version("meta-harness")
    except PackageNotFoundError:
        return "unknown"


@dataclass
class TaskOutcome:
    """What one candidate did on one labelled task."""

    task_name: str
    ticket_id: str
    expected: str
    actual: Optional[str]
    passed: bool
    confidence: float = 0.0
    error: str = ""
    rationale: str = ""
    seconds: float = 0.0

    @property
    def errored(self) -> bool:
        return self.actual is None

    def to_record(self) -> Dict:
        # `task_name`, `passed` and `reward` are what archive_reader normalizes;
        # the rest is diagnostic, kept because the first question about a
        # failure is always "what did it actually say?"
        return {
            "task_name": self.task_name,
            "passed": self.passed,
            "reward": 1.0 if self.passed else 0.0,
            "ticket_id": self.ticket_id,
            "expected_verdict": self.expected,
            "actual_verdict": self.actual,
            "confidence": self.confidence,
            "rationale": self.rationale,
            "error_summary": self.error,
            "seconds": round(self.seconds, 2),
        }


@dataclass
class EvaluationResult:
    """One candidate's whole run over a dataset."""

    candidate_name: str
    dataset_name: str
    run_dir: Path
    outcomes: List[TaskOutcome] = field(default_factory=list)

    @property
    def scored(self) -> List[TaskOutcome]:
        """Tasks that produced a verdict. A task that errored is not a wrong
        answer — counting it as one would let an unreachable tracker look like
        a worse prompt."""
        return [outcome for outcome in self.outcomes if not outcome.errored]

    @property
    def errors(self) -> List[TaskOutcome]:
        return [outcome for outcome in self.outcomes if outcome.errored]

    @property
    def pass_rate(self) -> float:
        scored = self.scored
        if not scored:
            return 0.0
        return sum(1 for outcome in scored if outcome.passed) / len(scored)

    def per_verdict_recall(self) -> Dict[str, Dict[str, int]]:
        """How each label fared, because overall accuracy hides the failure that
        matters: a candidate that never says `partially_related` can still score
        well while being useless at the only hard case."""
        breakdown: Dict[str, Dict[str, int]] = {}
        for outcome in self.scored:
            entry = breakdown.setdefault(outcome.expected, {"total": 0, "correct": 0})
            entry["total"] += 1
            if outcome.passed:
                entry["correct"] += 1
        return breakdown

    def confusions(self) -> Dict[str, int]:
        """Which mistakes were actually made, as `expected->actual` counts."""
        counts: Dict[str, int] = {}
        for outcome in self.scored:
            if outcome.passed:
                continue
            key = f"{outcome.expected}->{outcome.actual}"
            counts[key] = counts.get(key, 0) + 1
        return counts

    def summary_line(self) -> str:
        scored = self.scored
        passed = sum(1 for outcome in scored if outcome.passed)
        parts = [f"{passed}/{len(scored)} correct ({self.pass_rate:.0%})"]
        if self.errors:
            parts.append(f"{len(self.errors)} errored")
        return ", ".join(parts)


def evaluate_candidate(
    candidate: Candidate,
    dataset: Dataset,
    *,
    archive_root: Path,
    run_name: Optional[str] = None,
    on_step: OnStep = None,
) -> EvaluationResult:
    """Run one candidate over every task and write a run directory.

    The module context is retrieved once for the whole run. Retrieving per task
    would repeat an identical search and, worse, let the description drift
    between tasks — scoring the candidate against a moving target.
    """
    dataset.validate()
    candidate.validate()

    warning = dataset.imbalance_warning()
    if warning:
        _report(on_step, f"Note: {warning}")

    started_at = _now()
    module_context = dataset.module_context.strip()
    if not module_context:
        # `validate()` guarantees a repo when there is no pasted context.
        _report(on_step, f"Retrieving '{dataset.module_name}' from source '{dataset.repo}'…")
        module_context = retrieve_module_context(
            dataset.module_name,
            repo=dataset.repo,
            limit=candidate.context_limit,
            verified_only=candidate.verified_only,
            on_step=on_step,
        )
    rendered_context = candidate.render_context(module_context)

    run_dir = Path(archive_root).expanduser() / (run_name or _default_run_name(candidate, started_at))
    (run_dir / "tasks").mkdir(parents=True, exist_ok=True)

    outcomes: List[TaskOutcome] = []
    total = len(dataset.tasks)
    for index, task in enumerate(dataset.tasks, start=1):
        _report(on_step, f"[{index}/{total}] {task.task_name} ({task.ticket_id})…")
        task_started = time.monotonic()
        try:
            relevance = analyze_module_relevance(
                task.ticket_id,
                tracker=task.tracker,
                module_name=dataset.module_name,
                module_context=rendered_context,
                timeout_s=candidate.timeout_s,
            )
        except TASK_ERRORS as exc:
            outcomes.append(
                TaskOutcome(
                    task_name=task.task_name,
                    ticket_id=task.ticket_id,
                    expected=task.expected_verdict,
                    actual=None,
                    passed=False,
                    error=str(exc)[:300],
                    seconds=time.monotonic() - task_started,
                )
            )
            continue

        outcomes.append(
            TaskOutcome(
                task_name=task.task_name,
                ticket_id=task.ticket_id,
                expected=task.expected_verdict,
                actual=relevance.verdict,
                passed=relevance.verdict == task.expected_verdict,
                confidence=relevance.confidence,
                rationale=relevance.rationale[:400],
                seconds=time.monotonic() - task_started,
            )
        )

    result = EvaluationResult(
        candidate_name=candidate.name,
        dataset_name=dataset.name,
        run_dir=run_dir,
        outcomes=outcomes,
    )
    _write_run_dir(result, candidate, dataset, started_at)
    _report(on_step, f"Done: {result.summary_line()}")
    return result


def _default_run_name(candidate: Candidate, started_at: datetime) -> str:
    slug = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in candidate.name)
    return f"{slug}-{started_at.strftime('%Y%m%dT%H%M%SZ')}"


def _task_file_name(task_name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in task_name)


def _write_run_dir(
    result: EvaluationResult,
    candidate: Candidate,
    dataset: Dataset,
    started_at: datetime,
) -> None:
    """Emit the layout `archive_reader` already knows how to read."""
    run_dir = result.run_dir
    records = [outcome.to_record() for outcome in result.outcomes]
    for record in records:
        path = run_dir / "tasks" / f"{_task_file_name(record['task_name'])}.json"
        path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")

    scored = result.scored
    summary = {
        "benchmark_name": dataset.name,
        "candidate_name": candidate.name,
        # Upstream this is a path to a candidate file. There is no file here, so
        # it carries the settings fingerprint instead — the thing that actually
        # identifies what was run.
        "candidate_path": candidate.fingerprint(),
        "eval_metrics": {
            "eval/pass_rate": result.pass_rate,
            "eval/total_tasks": len(scored),
            "eval/passed_tasks": sum(1 for outcome in scored if outcome.passed),
            "eval/errored_tasks": len(result.errors),
            "eval/mean_seconds": (
                round(sum(o.seconds for o in result.outcomes) / len(result.outcomes), 2)
                if result.outcomes
                else 0.0
            ),
        },
        "task_results": records,
        "per_verdict": result.per_verdict_recall(),
        "confusions": result.confusions(),
    }
    (run_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    task_names = [task.task_name for task in dataset.tasks]
    manifest = {
        "benchmark_name": dataset.name,
        "candidate_name": candidate.name,
        "created_at": started_at.isoformat(),
        "outer_loop": {
            "benchmark_runner": {
                "schema_version": SCHEMA_VERSION,
                "benchmark": dataset.name,
                "candidate": candidate.name,
                "total_tasks": len(dataset.tasks),
                # Reused rather than reimplemented, so a dataset run and a
                # Hermes run hash their task selection the same way and
                # `comparison.py` can tell "same tasks" from "different tasks"
                # without knowing which produced the run.
                "task_selection": build_task_selection_metadata(
                    task_filter=",".join(task_names),
                    skip_tasks=None,
                ),
            },
            "provenance": {
                "metaharness_version": _package_version(),
                "capability": dataset.capability,
                "module_name": dataset.module_name,
                "repo": dataset.repo,
                "context_source": "repo" if not dataset.module_context.strip() else "pasted",
                "candidate": candidate.describe(),
                "label_counts": dataset.label_counts,
            },
        },
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
