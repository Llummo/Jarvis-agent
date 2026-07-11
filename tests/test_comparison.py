from pathlib import Path

from meta_harness.comparability import build_task_selection_metadata
from meta_harness.comparison import build_comparison_report, compare_runs
from meta_harness.models import RunSummary


def _task_selection_manifest(selection_hash):
    return {
        "outer_loop": {
            "benchmark_runner": {
                "task_selection": {
                    "selection_hash": selection_hash,
                }
            }
        }
    }


def test_compare_runs_detects_metric_and_task_deltas():
    baseline = RunSummary(
        benchmark_name="tblite",
        candidate_name="baseline",
        candidate_path="/tmp/base.py",
        run_dir=Path("/tmp/base"),
        eval_metrics={"eval/pass_rate": 0.2},
        task_results=[
            {"task_name": "a", "passed": False, "reward": 0.0},
            {"task_name": "b", "passed": True, "reward": 1.0},
        ],
    )
    candidate = RunSummary(
        benchmark_name="tblite",
        candidate_name="candidate",
        candidate_path="/tmp/candidate.py",
        run_dir=Path("/tmp/candidate"),
        eval_metrics={"eval/pass_rate": 0.3},
        task_results=[
            {"task_name": "a", "passed": True, "reward": 1.0},
            {"task_name": "b", "passed": True, "reward": 1.0},
        ],
    )

    comparison = compare_runs(baseline, candidate)
    assert comparison.metric_deltas["eval/pass_rate"] == 0.1
    statuses = {delta.task_name: delta.status for delta in comparison.task_deltas}
    assert statuses["a"] == "improved"
    assert statuses["b"] == "unchanged"


def test_build_comparison_report_summarizes_improvements():
    baseline = RunSummary(
        benchmark_name="tblite",
        candidate_name="baseline",
        candidate_path="/tmp/base.py",
        run_dir=Path("/tmp/base"),
        eval_metrics={
            "eval/pass_rate": 0.25,
            "eval/passed_tasks": 1,
            "eval/evaluation_time_seconds": 10.0,
        },
        task_results=[
            {"task_name": "a", "passed": False, "reward": 0.0},
            {"task_name": "b", "passed": True, "reward": 1.0},
        ],
    )
    candidate = RunSummary(
        benchmark_name="tblite",
        candidate_name="candidate",
        candidate_path="/tmp/candidate.py",
        run_dir=Path("/tmp/candidate"),
        eval_metrics={
            "eval/pass_rate": 0.50,
            "eval/passed_tasks": 2,
            "eval/evaluation_time_seconds": 8.0,
        },
        task_results=[
            {"task_name": "a", "passed": True, "reward": 1.0},
            {"task_name": "b", "passed": True, "reward": 1.0},
        ],
    )

    report = build_comparison_report(baseline, candidate)
    assert report.improved_tasks == 1
    assert report.regressed_tasks == 0
    assert report.net_task_gain == 1
    assert report.pass_rate_delta == 0.25
    assert report.passed_tasks_delta == 1.0
    assert report.evaluation_time_delta_seconds == -2.0
    assert report.improved_task_names == ["a"]
    assert report.candidate_better is True
    assert report.task_selection_status == "missing"


def test_build_comparison_report_marks_non_overlapping_tasks_incomparable():
    baseline = RunSummary(
        benchmark_name="tblite",
        candidate_name="baseline",
        candidate_path="/tmp/base.py",
        run_dir=Path("/tmp/base"),
        eval_metrics={"eval/pass_rate": 0.50, "eval/passed_tasks": 1},
        task_results=[
            {"task_name": "baseline_only", "passed": True, "reward": 1.0},
            {"task_name": "shared", "passed": False, "reward": 0.0},
        ],
    )
    candidate = RunSummary(
        benchmark_name="tblite",
        candidate_name="candidate",
        candidate_path="/tmp/candidate.py",
        run_dir=Path("/tmp/candidate"),
        eval_metrics={"eval/pass_rate": 0.75, "eval/passed_tasks": 2},
        task_results=[
            {"task_name": "candidate_only", "passed": True, "reward": 1.0},
            {"task_name": "shared", "passed": True, "reward": 1.0},
        ],
    )

    report = build_comparison_report(baseline, candidate)
    assert report.baseline_only_tasks == 1
    assert report.candidate_only_tasks == 1
    assert report.baseline_only_task_names == ["baseline_only"]
    assert report.candidate_only_task_names == ["candidate_only"]
    assert report.comparable_task_set is False
    assert report.candidate_better is False
    assert report.unchanged_task_names == []


def test_build_comparison_report_includes_task_diagnostics():
    baseline = RunSummary(
        benchmark_name="tblite",
        candidate_name="baseline",
        candidate_path="/tmp/base.py",
        run_dir=Path("/tmp/base"),
        eval_metrics={"eval/pass_rate": 0.5, "eval/passed_tasks": 1},
        task_results=[
            {
                "task_name": "a",
                "passed": True,
                "reward": 1.0,
                "trace_path": "baseline/a.jsonl",
            },
            {
                "task_name": "b",
                "passed": False,
                "reward": 0.0,
                "error_summary": "missing dependency",
            },
        ],
    )
    candidate = RunSummary(
        benchmark_name="tblite",
        candidate_name="candidate",
        candidate_path="/tmp/candidate.py",
        run_dir=Path("/tmp/candidate"),
        eval_metrics={"eval/pass_rate": 0.5, "eval/passed_tasks": 1},
        task_results=[
            {
                "task_name": "a",
                "passed": False,
                "reward": 0.0,
                "failure_reason": "premature final answer",
                "trace_path": "candidate/a.jsonl",
            },
            {
                "task_name": "b",
                "passed": True,
                "reward": 1.0,
                "trace_path": "candidate/b.jsonl",
            },
        ],
    )

    report = build_comparison_report(baseline, candidate)

    assert report.task_diagnostics == [
        {
            "task_name": "a",
            "status": "regressed",
            "baseline_error_summary": None,
            "candidate_error_summary": "premature final answer",
            "baseline_trace_path": "baseline/a.jsonl",
            "candidate_trace_path": "candidate/a.jsonl",
        },
        {
            "task_name": "b",
            "status": "improved",
            "baseline_error_summary": "missing dependency",
            "candidate_error_summary": None,
            "baseline_trace_path": None,
            "candidate_trace_path": "candidate/b.jsonl",
        },
    ]


def test_build_comparison_report_marks_task_selection_mismatch():
    baseline = RunSummary(
        benchmark_name="tblite",
        candidate_name="baseline",
        candidate_path="/tmp/base.py",
        run_dir=Path("/tmp/base"),
        eval_metrics={"eval/pass_rate": 0.25, "eval/passed_tasks": 1},
        task_results=[{"task_name": "a", "passed": False, "reward": 0.0}],
        manifest=_task_selection_manifest("hash_a"),
    )
    candidate = RunSummary(
        benchmark_name="tblite",
        candidate_name="candidate",
        candidate_path="/tmp/candidate.py",
        run_dir=Path("/tmp/candidate"),
        eval_metrics={"eval/pass_rate": 1.0, "eval/passed_tasks": 1},
        task_results=[{"task_name": "a", "passed": True, "reward": 1.0}],
        manifest=_task_selection_manifest("hash_b"),
    )

    report = build_comparison_report(baseline, candidate)

    assert report.task_selection_status == "mismatched"
    assert report.baseline_task_selection_hash == "hash_a"
    assert report.candidate_task_selection_hash == "hash_b"
    assert report.comparable_task_selection is False
    assert report.candidate_better is False


def test_build_comparison_report_marks_task_selection_match():
    selection = build_task_selection_metadata(task_filter="a,b", skip_tasks=None)
    manifest = {
        "outer_loop": {
            "benchmark_runner": {
                "task_selection": selection,
            }
        }
    }
    baseline = RunSummary(
        benchmark_name="tblite",
        candidate_name="baseline",
        candidate_path="/tmp/base.py",
        run_dir=Path("/tmp/base"),
        eval_metrics={"eval/pass_rate": 0.25, "eval/passed_tasks": 1},
        task_results=[{"task_name": "a", "passed": False, "reward": 0.0}],
        manifest=manifest,
    )
    candidate = RunSummary(
        benchmark_name="tblite",
        candidate_name="candidate",
        candidate_path="/tmp/candidate.py",
        run_dir=Path("/tmp/candidate"),
        eval_metrics={"eval/pass_rate": 1.0, "eval/passed_tasks": 1},
        task_results=[{"task_name": "a", "passed": True, "reward": 1.0}],
        manifest=manifest,
    )

    report = build_comparison_report(baseline, candidate)

    assert report.task_selection_status == "matching"
    assert report.baseline_task_selection_hash == selection["selection_hash"]
    assert report.candidate_better is True
