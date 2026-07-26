from pathlib import Path

from click.testing import CliRunner

from meta_harness.benchmark_runner import BenchmarkRunResult
from meta_harness.cli import main
from meta_harness.frontier import FrontierStore
from meta_harness.models import RunSummary, SearchSummary, SearchTrialResult


def test_show_frontier_cli_lists_ranked_entries(tmp_path):
    frontier = FrontierStore(tmp_path / "frontier.json")
    frontier.upsert_from_summary(
        RunSummary(
            benchmark_name="tblite",
            candidate_name="strong_candidate",
            candidate_path="/tmp/strong.py",
            run_dir=Path("/tmp/strong"),
            eval_metrics={"eval/pass_rate": 0.8, "eval/total_tasks": 20},
        )
    )

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "show-frontier",
            "--frontier-path",
            str(frontier.path),
            "--benchmark",
            "tblite",
        ],
    )

    assert result.exit_code == 0
    assert "strong_candidate" in result.output
    assert "20" in result.output


def test_evaluate_vs_baseline_cli_dry_run(monkeypatch, tmp_path):
    hermes_repo = tmp_path / "hermes-agent"
    hermes_repo.mkdir()
    calls = []

    def fake_run_benchmark(config, run_spec, *, dry_run=False, timeout=None):
        calls.append(run_spec.candidate)
        return BenchmarkRunResult(
            command=["python3", str(run_spec.candidate), "--dry-run"],
            archive_root=run_spec.archive_root,
            returncode=0,
        )

    monkeypatch.setattr("meta_harness.cli.run_benchmark", fake_run_benchmark)

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "evaluate-vs-baseline",
            "--candidate",
            "candidates/template_candidate.py",
            "--benchmark",
            "tblite",
            "--hermes-repo",
            str(hermes_repo),
            "--dry-run",
        ],
    )

    assert result.exit_code == 0
    assert "Baseline vs Candidate Evaluation" in result.output
    assert "fresh_candidate" in result.output
    assert len(calls) == 2


def test_search_candidates_cli_dry_run(monkeypatch, tmp_path):
    hermes_repo = tmp_path / "hermes-agent"
    hermes_repo.mkdir()

    def fake_run_structured_search(config, request, *, dry_run=False):
        assert dry_run is True
        return SearchSummary(
            benchmark_name=request.benchmark,
            baseline_candidate=request.baseline_candidate,
            baseline_source="fresh_candidate",
            baseline_run_dir=None,
            baseline_reference=None,
            seed_candidate=request.seed_candidate,
            workspace_dir=str(tmp_path / "workspace"),
            generated_candidates_dir=str(tmp_path / "workspace" / "generated_candidates"),
            trial_results=[
                SearchTrialResult(
                    mutation_slug="plan_briefly",
                    mutation_description="Prepend a brief planning reminder.",
                    candidate_name="seed__plan_briefly",
                    candidate_path=str(tmp_path / "workspace" / "generated_candidates" / "seed__plan_briefly.py"),
                )
            ],
        )

    monkeypatch.setattr("meta_harness.cli.run_structured_search", fake_run_structured_search)

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "search-candidates",
            "--seed-candidate",
            "candidates/template_candidate.py",
            "--benchmark",
            "tblite",
            "--hermes-repo",
            str(hermes_repo),
            "--dry-run",
        ],
    )

    assert result.exit_code == 0
    assert "Structured Search" in result.output
    assert "fresh_candidate" in result.output
    assert "seed__plan_briefly" in result.output


def test_evaluate_candidate_cli_supports_launcher_prefix(monkeypatch, tmp_path):
    hermes_repo = tmp_path / "hermes-agent"
    hermes_repo.mkdir()
    captured = {}

    def fake_run_benchmark(config, run_spec, *, dry_run=False, timeout=None):
        captured["launch_prefix"] = config.launch_prefix
        captured["python_executable"] = config.python_executable
        return BenchmarkRunResult(
            command=["uv", "run", "--python", "3.12", "--extra", "rl", "python", "bench.py"],
            archive_root=run_spec.archive_root,
            returncode=0,
        )

    monkeypatch.setattr("meta_harness.cli.run_benchmark", fake_run_benchmark)

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "evaluate-candidate",
            "--candidate",
            "snapshot_baseline",
            "--benchmark",
            "tblite",
            "--hermes-repo",
            str(hermes_repo),
            "--launcher-prefix",
            "uv run --python 3.12 --extra rl",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0
    assert captured["launch_prefix"] == ("uv", "run", "--python", "3.12", "--extra", "rl")
    assert captured["python_executable"] == "python"


def test_evaluate_candidate_cli_reports_runtime_errors(monkeypatch, tmp_path):
    hermes_repo = tmp_path / "hermes-agent"
    hermes_repo.mkdir()

    def fake_run_benchmark(config, run_spec, *, dry_run=False, timeout=None):
        raise RuntimeError("benchmark exploded")

    monkeypatch.setattr("meta_harness.cli.run_benchmark", fake_run_benchmark)

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "evaluate-candidate",
            "--candidate",
            "snapshot_baseline",
            "--benchmark",
            "tblite",
            "--hermes-repo",
            str(hermes_repo),
        ],
    )

    assert result.exit_code != 0
    assert "Error: benchmark exploded" in result.output
    assert "Traceback" not in result.output


def test_search_candidates_cli_reports_unknown_mutation(tmp_path):
    hermes_repo = tmp_path / "hermes-agent"
    hermes_repo.mkdir()
    seed_candidate = tmp_path / "seed.py"
    seed_candidate.write_text("# seed\n", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "search-candidates",
            "--seed-candidate",
            str(seed_candidate),
            "--benchmark",
            "tblite",
            "--hermes-repo",
            str(hermes_repo),
            "--workspace-dir",
            str(tmp_path / "workspace"),
            "--mutation",
            "does_not_exist",
            "--dry-run",
        ],
    )

    assert result.exit_code != 0
    assert "Unknown mutation" in result.output
    assert "Traceback" not in result.output


def test_search_candidates_cli_reports_runtime_errors(monkeypatch, tmp_path):
    hermes_repo = tmp_path / "hermes-agent"
    hermes_repo.mkdir()
    seed_candidate = tmp_path / "seed.py"
    seed_candidate.write_text("# seed\n", encoding="utf-8")

    def fake_run_structured_search(config, request, *, dry_run=False):
        raise RuntimeError("baseline failed")

    monkeypatch.setattr("meta_harness.cli.run_structured_search", fake_run_structured_search)

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "search-candidates",
            "--seed-candidate",
            str(seed_candidate),
            "--benchmark",
            "tblite",
            "--hermes-repo",
            str(hermes_repo),
        ],
    )

    assert result.exit_code != 0
    assert "Error: baseline failed" in result.output
    assert "Traceback" not in result.output


def test_qa_report_issue_cli_minor(tmp_path):
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "qa", "report-issue",
            "--project", "sigo-front",
            "--route", "/checkout",
            "--observation", "misaligned button",
            "--severity", "minor",
            "--db-path", str(tmp_path / "findings.db"),
        ],
    )

    assert result.exit_code == 0
    assert "open" in result.output


def test_qa_report_issue_cli_critical_creates_ticket(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "meta_harness.qa_findings.clickup_bridge.create_clickup_ticket", lambda **kw: "CU-1"
    )
    runner = CliRunner(env={"COLUMNS": "200"})
    result = runner.invoke(
        main,
        [
            "qa", "report-issue",
            "--project", "sigo-front",
            "--route", "/pay",
            "--observation", "500 on submit",
            "--severity", "critical",
            "--db-path", str(tmp_path / "findings.db"),
        ],
    )

    assert result.exit_code == 0
    assert "acknowledged" in result.output
    assert "CU-1" in result.output


def test_qa_report_issue_cli_rejects_invalid_severity(tmp_path):
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "qa", "report-issue",
            "--project", "p",
            "--route", "/r",
            "--observation", "o",
            "--severity", "blocker",
            "--db-path", str(tmp_path / "findings.db"),
        ],
    )

    assert result.exit_code != 0


def test_qa_list_issues_cli_filters_by_severity(tmp_path):
    runner = CliRunner(env={"COLUMNS": "200"})
    db_path = str(tmp_path / "findings.db")
    runner.invoke(main, [
        "qa", "report-issue", "--project", "p", "--route", "/a",
        "--observation", "obs a", "--severity", "minor", "--db-path", db_path,
    ])
    runner.invoke(main, [
        "qa", "report-issue", "--project", "p", "--route", "/b",
        "--observation", "obs b", "--severity", "major", "--db-path", db_path,
    ])

    result = runner.invoke(main, ["qa", "list-issues", "--severity", "major", "--db-path", db_path])

    assert result.exit_code == 0
    assert "/b" in result.output
    assert "/a" not in result.output


def test_qa_close_issue_cli(tmp_path):
    runner = CliRunner(env={"COLUMNS": "200"})
    db_path = str(tmp_path / "findings.db")
    runner.invoke(main, [
        "qa", "report-issue", "--project", "p", "--route", "/a",
        "--observation", "obs a", "--severity", "minor", "--db-path", db_path,
    ])

    result = runner.invoke(main, ["qa", "close-issue", "1", "--note", "fixed it", "--db-path", db_path])

    assert result.exit_code == 0
    assert "closed" in result.output
