"""CLI for standalone Hermes Meta-Harness orchestration."""

from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Iterable, Optional

import click
from rich.console import Console
from rich.table import Table

from meta_harness.archive_reader import load_run_summary
from meta_harness.baseline import resolve_baseline_selection
from meta_harness.benchmark_runner import BenchmarkRunResult, run_benchmark
from meta_harness.candidate_registry import list_builtin_candidates
from meta_harness.clickup_bridge import (
    CLICKUP_PRIORITY_WORDS,
    ClickUpReadError,
    ClickUpTicketError,
    create_clickup_ticket,
    get_clickup_task,
    list_clickup_folders,
    list_clickup_lists,
    list_clickup_spaces,
    list_clickup_tasks,
    list_clickup_teams,
    update_clickup_task,
    update_clickup_task_status,
)
from meta_harness.comparison import build_comparison_report
from meta_harness.config import MetaHarnessConfig, parse_command_prefix
from meta_harness.doctor import run_all
from meta_harness.embeddings import (
    EmbeddingError,
    SourceError,
    VectorStore,
    VectorStoreError,
    hybrid_search,
    index_repository,
    resolve_embedder,
    source_status,
)
from meta_harness.frontier import FrontierStore
from meta_harness.hermes_compat import inspect_hermes_compatibility
from meta_harness.linear_bridge import (
    LINEAR_PRIORITY_WORDS,
    LinearIssueError,
    LinearReadError,
    create_linear_issue,
    get_linear_issue,
    get_linear_viewer,
    list_linear_issues,
    list_linear_members,
    list_linear_projects,
    list_linear_states,
    list_linear_teams,
    update_linear_issue,
    update_linear_issue_state,
)
from meta_harness.models import BenchmarkRunSpec
from meta_harness.mutation import builtin_mutations, safe_slug
from meta_harness.playbook import (
    AgentPlaybook,
    flow_requires_subject,
    init_project,
    list_agent_playbooks,
    load_agent_playbook,
    replay_run,
    resolve_project_path,
    run_and_record_flow,
)
from meta_harness.qa_findings import (
    SEVERITIES,
    STATUSES,
    QAFinding,
    QAFindingNotFoundError,
    QAFindingStore,
    close_qa_issue,
    list_qa_issues,
    report_qa_issue,
)
from meta_harness.qa_flow import QAFlowError, replay_qa_review, review_and_record
from meta_harness.run_archive import RunArchive
from meta_harness.search import StructuredSearchRequest, run_structured_search
from meta_harness.ticket_generator import (
    ClaudeNotFoundError,
    ProposedTicket,
    TicketExtractionError,
    TicketGenerationError,
    TicketParseError,
    apply_ticket_numbering,
    generate_tickets_from_file,
)
from meta_harness.trackers.config import ClickUpConfig

console = Console()


@click.group()
def main() -> None:
    """Standalone outer-loop Meta-Harness tooling for Hermes."""


def _format_optional_delta(value: Optional[float]) -> str:
    if value is None:
        return "-"
    return f"{value:+.4f}"


def _emit_report(report, *, show_task_names: bool = True) -> None:
    summary_table = Table(title="Baseline vs Candidate")
    summary_table.add_column("Metric", style="bold")
    summary_table.add_column("Value", justify="right")
    summary_table.add_row("Benchmark", report.benchmark_name)
    summary_table.add_row("Baseline", report.baseline_candidate_name)
    summary_table.add_row("Candidate", report.candidate_name)
    summary_table.add_row("Comparable Task Set", "yes" if report.comparable_task_set else "no")
    summary_table.add_row("Task Selection", report.task_selection_status)
    summary_table.add_row("Candidate Better", "yes" if report.candidate_better else "no")
    summary_table.add_row("Pass Rate Delta", _format_optional_delta(report.pass_rate_delta))
    summary_table.add_row("Passed Tasks Delta", _format_optional_delta(report.passed_tasks_delta))
    summary_table.add_row(
        "Eval Time Delta (s)",
        _format_optional_delta(report.evaluation_time_delta_seconds),
    )
    summary_table.add_row("Improved Tasks", str(report.improved_tasks))
    summary_table.add_row("Regressed Tasks", str(report.regressed_tasks))
    summary_table.add_row("Net Task Gain", str(report.net_task_gain))
    summary_table.add_row("Overlapping Tasks", str(report.overlapping_tasks))
    summary_table.add_row("Total Compared Tasks", str(report.total_tasks))
    summary_table.add_row("Baseline Run", str(report.baseline_run_dir))
    summary_table.add_row("Candidate Run", str(report.candidate_run_dir))
    console.print()
    console.print(summary_table)

    if report.metric_deltas:
        metrics_table = Table(title="Metric Deltas")
        metrics_table.add_column("Metric", style="bold")
        metrics_table.add_column("Delta", justify="right")
        for key, delta in sorted(report.metric_deltas.items()):
            metrics_table.add_row(key, f"{delta:+.4f}")
        console.print()
        console.print(metrics_table)

    if not show_task_names:
        return

    if report.regressed_task_names:
        regressed_table = Table(title="Regressed Tasks")
        regressed_table.add_column("Task", style="bold red")
        for task_name in report.regressed_task_names:
            regressed_table.add_row(task_name)
        console.print()
        console.print(regressed_table)

    if report.improved_task_names:
        improved_table = Table(title="Improved Tasks")
        improved_table.add_column("Task", style="bold green")
        for task_name in report.improved_task_names:
            improved_table.add_row(task_name)
        console.print()
        console.print(improved_table)

    if report.baseline_only_task_names:
        baseline_only_table = Table(title="Baseline-Only Tasks")
        baseline_only_table.add_column("Task", style="bold yellow")
        for task_name in report.baseline_only_task_names:
            baseline_only_table.add_row(task_name)
        console.print()
        console.print(baseline_only_table)

    if report.candidate_only_task_names:
        candidate_only_table = Table(title="Candidate-Only Tasks")
        candidate_only_table.add_column("Task", style="bold yellow")
        for task_name in report.candidate_only_task_names:
            candidate_only_table.add_row(task_name)
        console.print()
        console.print(candidate_only_table)

    if report.task_diagnostics:
        diagnostics_table = Table(title="Task Diagnostics")
        diagnostics_table.add_column("Task", style="bold")
        diagnostics_table.add_column("Status")
        diagnostics_table.add_column("Baseline Error")
        diagnostics_table.add_column("Candidate Error")
        diagnostics_table.add_column("Baseline Trace")
        diagnostics_table.add_column("Candidate Trace")
        for item in report.task_diagnostics:
            diagnostics_table.add_row(
                str(item.get("task_name") or "-"),
                str(item.get("status") or "-"),
                str(item.get("baseline_error_summary") or "-"),
                str(item.get("candidate_error_summary") or "-"),
                str(item.get("baseline_trace_path") or "-"),
                str(item.get("candidate_trace_path") or "-"),
            )
        console.print()
        console.print(diagnostics_table)


def _build_config(
    hermes_repo: Optional[str],
    *,
    launcher_prefix: Optional[str] = None,
    python_executable: Optional[str] = None,
) -> MetaHarnessConfig:
    try:
        if hermes_repo:
            config = MetaHarnessConfig(hermes_agent_path=Path(hermes_repo).expanduser().resolve())
        else:
            config = MetaHarnessConfig()
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc
    if launcher_prefix is not None:
        config.launch_prefix = parse_command_prefix(launcher_prefix)
        if python_executable is None:
            config.python_executable = "python" if config.launch_prefix else config.python_executable
    if python_executable is not None:
        config.python_executable = python_executable
    return config


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _format_command(command: Iterable[str], *, fallback: str) -> str:
    rendered = " ".join(command).strip()
    return rendered if rendered else fallback


def _clickify_runtime_error(exc: Exception) -> click.ClickException:
    return click.ClickException(str(exc))


@main.command("check-hermes")
@click.option("--hermes-repo", type=click.Path(exists=True, file_okay=False), help="Path to the hermes-agent checkout.")
@click.option("--json-output", help="Optional path to write the compatibility report as JSON.")
def check_hermes_cmd(hermes_repo: Optional[str] = None, json_output: Optional[str] = None) -> None:
    """Check whether a Hermes checkout supports this Meta-Harness repo."""
    config = _build_config(hermes_repo)
    report = inspect_hermes_compatibility(config.hermes_agent_path)

    overview = Table(title="Hermes Compatibility")
    overview.add_column("Field", style="bold")
    overview.add_column("Value")
    overview.add_row("Hermes Repo", str(report.hermes_agent_path))
    overview.add_row("Remote", report.remote_url or "-")
    overview.add_row("Git Head", report.git_head or "-")
    overview.add_row("Compatible", "yes" if report.compatible else "no")
    overview.add_row("Built-in Candidates", ", ".join(report.builtin_candidates) or "-")
    console.print(overview)

    checks = Table(title="Required Legacy Surface")
    checks.add_column("Component", style="bold")
    checks.add_column("Path")
    checks.add_column("Status", justify="right")
    missing_paths = {issue.path for issue in report.issues if issue.severity == "error"}
    for benchmark, path in report.benchmark_scripts.items():
        relative_path = str(Path(path).relative_to(report.hermes_agent_path))
        checks.add_row(f"benchmark:{benchmark}", relative_path, "missing" if relative_path in missing_paths else "ok")
    for name, path in report.runtime_files.items():
        relative_path = str(Path(path).relative_to(report.hermes_agent_path))
        checks.add_row(f"runtime:{name}", relative_path, "missing" if relative_path in missing_paths else "ok")
    console.print()
    console.print(checks)

    if report.issues:
        issues_table = Table(title="Issues")
        issues_table.add_column("Severity", style="bold")
        issues_table.add_column("Code")
        issues_table.add_column("Path")
        issues_table.add_column("Message")
        for issue in report.issues:
            issues_table.add_row(issue.severity, issue.code, issue.path, issue.message)
        console.print()
        console.print(issues_table)
        console.print(
            "\nIssue codes: "
            + ", ".join(sorted({issue.code for issue in report.issues}))
        )

    if json_output:
        _write_json(Path(json_output).expanduser().resolve(), report.to_dict())

    if not report.compatible:
        raise click.ClickException(
            "Hermes checkout is missing the legacy Meta-Harness benchmark/runtime surface required by this repo."
        )


@main.command("list-builtins")
@click.option("--hermes-repo", type=click.Path(exists=True, file_okay=False), help="Path to the hermes-agent checkout.")
def list_builtins_cmd(hermes_repo: Optional[str] = None) -> None:
    """List Hermes built-in Meta-Harness candidates."""
    config = _build_config(hermes_repo)

    for name in list_builtin_candidates(config.hermes_agent_path):
        console.print(name)


@main.command("list-mutations")
def list_mutations_cmd() -> None:
    """List built-in structured search mutations."""
    table = Table(title="Built-in Mutations")
    table.add_column("Slug", style="bold")
    table.add_column("Description")
    for slug, mutation in builtin_mutations().items():
        table.add_row(slug, mutation.description)
    console.print(table)


@main.command("show-frontier")
@click.option("--frontier-path", required=True, type=click.Path(exists=True, dir_okay=False), help="Path to a frontier JSON file.")
@click.option("--benchmark", required=True, help="Benchmark name to inspect.")
@click.option("--limit", default=10, show_default=True, type=int, help="Maximum entries to show.")
def show_frontier_cmd(frontier_path: str, benchmark: str, limit: int) -> None:
    """Show ranked frontier entries for one benchmark."""
    frontier = FrontierStore(Path(frontier_path).expanduser().resolve())
    entries = frontier.top_for_benchmark(benchmark, limit=limit)
    if not entries:
        raise click.ClickException(f"No frontier entries found for benchmark '{benchmark}'.")

    table = Table(title=f"Frontier: {benchmark}")
    table.add_column("Rank", justify="right")
    table.add_column("Candidate", style="bold")
    table.add_column("Total Tasks", justify="right")
    table.add_column("Pass Rate", justify="right")
    table.add_column("Status")
    table.add_column("Run Dir")
    for index, entry in enumerate(entries, start=1):
        table.add_row(
            str(index),
            entry.candidate_name,
            str(entry.total_tasks),
            f"{entry.pass_rate:.4f}",
            entry.status,
            entry.run_dir,
        )
    console.print(table)


@main.command("evaluate-candidate")
@click.option("--candidate", required=True, help="Candidate file path or built-in Hermes candidate name.")
@click.option("--benchmark", default="tblite", type=click.Choice(["tblite", "tb2"]), show_default=True)
@click.option("--hermes-repo", type=click.Path(exists=True, file_okay=False), help="Path to the hermes-agent checkout.")
@click.option("--launcher-prefix", help="Shell-style prefix used to launch Hermes, e.g. 'uv run --python 3.12 --extra rl'.")
@click.option("--python-executable", help="Python command executed inside the launcher prefix. Defaults to 'python' when a launcher prefix is set.")
@click.option("--archive-dir", help="Archive root for Hermes Meta-Harness run outputs.")
@click.option("--run-name", help="Optional friendly run name.")
@click.option("--hermes-config-path", type=click.Path(exists=True, dir_okay=False), help="Optional Hermes benchmark YAML config path.")
@click.option("--task-filter", help="Optional comma-separated task filter.")
@click.option("--skip-tasks", help="Optional comma-separated task skip list.")
@click.option("--frontier-path", help="Optional path to a frontier JSON file to update after evaluation.")
@click.option("--dry-run", is_flag=True, help="Print the benchmark command without executing it.")
def evaluate_candidate_cmd(
    candidate: str,
    benchmark: str,
    hermes_repo: Optional[str] = None,
    launcher_prefix: Optional[str] = None,
    python_executable: Optional[str] = None,
    archive_dir: Optional[str] = None,
    run_name: Optional[str] = None,
    hermes_config_path: Optional[str] = None,
    task_filter: Optional[str] = None,
    skip_tasks: Optional[str] = None,
    frontier_path: Optional[str] = None,
    dry_run: bool = False,
) -> None:
    """Evaluate one Meta-Harness candidate against Hermes."""
    config = _build_config(
        hermes_repo,
        launcher_prefix=launcher_prefix,
        python_executable=python_executable,
    )

    archive_root = Path(archive_dir).expanduser().resolve() if archive_dir else (
        config.output_dir / "archives" / benchmark
    )

    run_spec = BenchmarkRunSpec(
        benchmark=benchmark,
        candidate=candidate,
        archive_root=archive_root,
        run_name=run_name,
        hermes_config_path=Path(hermes_config_path).expanduser().resolve() if hermes_config_path else None,
        task_filter=task_filter,
        skip_tasks=skip_tasks,
        python_executable=config.python_executable,
    )

    console.print(f"\n[bold cyan]Meta-Harness Candidate Evaluation[/bold cyan]")
    console.print(f"  Hermes repo: {config.hermes_agent_path}")
    console.print(f"  Benchmark: {benchmark}")
    console.print(f"  Candidate: {candidate}")
    console.print(f"  Archive root: {archive_root}")

    try:
        result = run_benchmark(config, run_spec, dry_run=dry_run)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        raise _clickify_runtime_error(exc) from exc

    console.print("\n[bold]Command[/bold]")
    console.print("  " + " ".join(result.command))

    if dry_run:
        console.print("\n[yellow]Dry run only — benchmark not executed.[/yellow]")
        return

    if result.summary is None:
        console.print("\n[red]No summary was produced.[/red]")
        return

    summary = result.summary
    table = Table(title="Benchmark Summary")
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")

    pass_rate = summary.eval_metrics.get("eval/pass_rate")
    total_tasks = summary.eval_metrics.get("eval/total_tasks")
    passed_tasks = summary.eval_metrics.get("eval/passed_tasks")
    eval_time = summary.eval_metrics.get("eval/evaluation_time_seconds")

    table.add_row("Candidate", summary.candidate_name)
    table.add_row("Benchmark", summary.benchmark_name)
    table.add_row("Pass Rate", f"{pass_rate:.4f}" if isinstance(pass_rate, (int, float)) else str(pass_rate))
    table.add_row("Passed / Total", f"{passed_tasks}/{total_tasks}")
    table.add_row("Eval Time (s)", f"{eval_time:.1f}" if isinstance(eval_time, (int, float)) else str(eval_time))
    table.add_row("Run Dir", str(summary.run_dir))
    console.print()
    console.print(table)

    if frontier_path:
        frontier = FrontierStore(Path(frontier_path))
        entry = frontier.upsert_from_summary(summary)
        console.print(f"\n[green]Frontier updated:[/green] {entry.candidate_name} @ {entry.pass_rate:.4f}")


@main.command("compare-runs")
@click.option("--baseline-run", required=True, help="Path to the baseline Hermes Meta-Harness run dir.")
@click.option("--candidate-run", required=True, help="Path to the candidate Hermes Meta-Harness run dir.")
@click.option("--json-output", help="Optional path to write the comparison report as JSON.")
@click.option("--hide-task-names", is_flag=True, help="Hide improved/regressed task name tables.")
def compare_runs_cmd(
    baseline_run: str,
    candidate_run: str,
    json_output: Optional[str] = None,
    hide_task_names: bool = False,
) -> None:
    """Compare two Hermes Meta-Harness run directories."""
    baseline = load_run_summary(Path(baseline_run))
    candidate = load_run_summary(Path(candidate_run))
    report = build_comparison_report(baseline, candidate)
    _emit_report(report, show_task_names=not hide_task_names)

    if json_output:
        _write_json(Path(json_output).expanduser().resolve(), report.to_dict())


@main.command("evaluate-vs-baseline")
@click.option("--candidate", required=True, help="Candidate file path or built-in Hermes candidate name.")
@click.option("--baseline-candidate", default="snapshot_baseline", show_default=True)
@click.option("--baseline-run", type=click.Path(exists=True, file_okay=False), help="Reuse an existing baseline run directory instead of running one.")
@click.option("--baseline-from-frontier", type=click.Path(exists=True, dir_okay=False), help="Reuse the current best frontier entry for this benchmark as baseline.")
@click.option("--benchmark", default="tblite", type=click.Choice(["tblite", "tb2"]), show_default=True)
@click.option("--hermes-repo", type=click.Path(exists=True, file_okay=False), help="Path to the hermes-agent checkout.")
@click.option("--launcher-prefix", help="Shell-style prefix used to launch Hermes, e.g. 'uv run --python 3.12 --extra rl'.")
@click.option("--python-executable", help="Python command executed inside the launcher prefix. Defaults to 'python' when a launcher prefix is set.")
@click.option("--archive-dir", help="Archive root for the paired evaluation.")
@click.option("--candidate-run-name", help="Optional candidate run name.")
@click.option("--baseline-run-name", help="Optional baseline run name.")
@click.option("--hermes-config-path", type=click.Path(exists=True, dir_okay=False), help="Optional Hermes benchmark YAML config path.")
@click.option("--task-filter", help="Optional comma-separated task filter.")
@click.option("--skip-tasks", help="Optional comma-separated task skip list.")
@click.option("--frontier-path", help="Optional frontier JSON path to update with the candidate run.")
@click.option("--json-output", help="Optional path to write the comparison report as JSON.")
@click.option("--dry-run", is_flag=True, help="Print benchmark commands without executing them.")
def evaluate_vs_baseline_cmd(
    candidate: str,
    benchmark: str,
    baseline_candidate: str,
    baseline_run: Optional[str] = None,
    baseline_from_frontier: Optional[str] = None,
    hermes_repo: Optional[str] = None,
    launcher_prefix: Optional[str] = None,
    python_executable: Optional[str] = None,
    archive_dir: Optional[str] = None,
    candidate_run_name: Optional[str] = None,
    baseline_run_name: Optional[str] = None,
    hermes_config_path: Optional[str] = None,
    task_filter: Optional[str] = None,
    skip_tasks: Optional[str] = None,
    frontier_path: Optional[str] = None,
    json_output: Optional[str] = None,
    dry_run: bool = False,
) -> None:
    """Run a baseline and candidate, then emit a richer comparison report."""
    config = _build_config(
        hermes_repo,
        launcher_prefix=launcher_prefix,
        python_executable=python_executable,
    )
    archive_root = Path(archive_dir).expanduser().resolve() if archive_dir else (
        config.output_dir / "comparisons" / benchmark
    )
    try:
        baseline_selection = resolve_baseline_selection(
            benchmark_name=benchmark,
            baseline_candidate=baseline_candidate,
            baseline_run_dir=Path(baseline_run).expanduser().resolve() if baseline_run else None,
            baseline_frontier_path=Path(baseline_from_frontier).expanduser().resolve() if baseline_from_frontier else None,
            task_filter=task_filter,
            skip_tasks=skip_tasks,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    common_kwargs = {
        "benchmark": benchmark,
        "archive_root": archive_root,
        "hermes_config_path": Path(hermes_config_path).expanduser().resolve() if hermes_config_path else None,
        "task_filter": task_filter,
        "skip_tasks": skip_tasks,
        "python_executable": config.python_executable,
    }
    candidate_spec = BenchmarkRunSpec(
        candidate=candidate,
        run_name=candidate_run_name or f"candidate__{safe_slug(candidate)}",
        **common_kwargs,
    )

    console.print(f"\n[bold cyan]Baseline vs Candidate Evaluation[/bold cyan]")
    console.print(f"  Hermes repo: {config.hermes_agent_path}")
    console.print(f"  Benchmark: {benchmark}")
    console.print(f"  Baseline: {baseline_selection.display_label}")
    console.print(f"  Baseline source: {baseline_selection.source}")
    console.print(f"  Candidate: {candidate}")
    console.print(f"  Archive root: {archive_root}")

    if baseline_selection.requires_fresh_run:
        baseline_spec = BenchmarkRunSpec(
            candidate=baseline_selection.baseline_candidate,
            run_name=baseline_run_name or f"baseline__{safe_slug(baseline_selection.baseline_candidate)}",
            **common_kwargs,
        )
        try:
            baseline_result = run_benchmark(config, baseline_spec, dry_run=dry_run)
        except (FileNotFoundError, RuntimeError, ValueError) as exc:
            raise _clickify_runtime_error(exc) from exc
    else:
        baseline_result = BenchmarkRunResult(
            command=[],
            archive_root=archive_root,
            returncode=0,
            run_dir=baseline_selection.run_dir,
            summary=baseline_selection.summary,
        )
    try:
        candidate_result = run_benchmark(config, candidate_spec, dry_run=dry_run)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        raise _clickify_runtime_error(exc) from exc

    command_table = Table(title="Commands")
    command_table.add_column("Run", style="bold")
    command_table.add_column("Command")
    command_table.add_row(
        "Baseline",
        _format_command(
            baseline_result.command,
            fallback=f"(reused {baseline_selection.source}: {baseline_selection.reference or baseline_selection.display_label})",
        ),
    )
    command_table.add_row("Candidate", _format_command(candidate_result.command, fallback="(no command)"))
    console.print()
    console.print(command_table)

    if dry_run:
        console.print("\n[yellow]Dry run only — benchmark not executed.[/yellow]")
        return

    if baseline_result.summary is None or candidate_result.summary is None:
        raise click.ClickException("Expected both baseline and candidate summaries.")

    report = build_comparison_report(baseline_result.summary, candidate_result.summary)
    _emit_report(report)

    if json_output:
        _write_json(Path(json_output).expanduser().resolve(), report.to_dict())

    if frontier_path:
        frontier = FrontierStore(Path(frontier_path))
        entry = frontier.upsert_from_summary(
            candidate_result.summary,
            status="candidate_beats_baseline" if report.candidate_better else "evaluated",
            notes=(
                f"baseline={baseline_selection.baseline_candidate}; pass_rate_delta={report.pass_rate_delta:+.4f}; "
                f"net_task_gain={report.net_task_gain}; regressions={report.regressed_tasks}"
            ),
        )
        console.print(f"\n[green]Frontier updated:[/green] {entry.candidate_name} @ {entry.pass_rate:.4f}")


@main.command("search-candidates")
@click.option("--seed-candidate", required=True, help="Seed candidate file path or built-in Hermes candidate name.")
@click.option("--baseline-candidate", default="snapshot_baseline", show_default=True)
@click.option("--baseline-run", type=click.Path(exists=True, file_okay=False), help="Reuse an existing baseline run directory instead of running one.")
@click.option("--baseline-from-frontier", type=click.Path(exists=True, dir_okay=False), help="Reuse the current best frontier entry for this benchmark as baseline.")
@click.option("--benchmark", default="tblite", type=click.Choice(["tblite", "tb2"]), show_default=True)
@click.option("--hermes-repo", type=click.Path(exists=True, file_okay=False), help="Path to the hermes-agent checkout.")
@click.option("--launcher-prefix", help="Shell-style prefix used to launch Hermes, e.g. 'uv run --python 3.12 --extra rl'.")
@click.option("--python-executable", help="Python command executed inside the launcher prefix. Defaults to 'python' when a launcher prefix is set.")
@click.option("--workspace-dir", help="Workspace directory for generated candidates, reports, and search summary.")
@click.option("--archive-dir", help="Archive root for evaluations inside this search.")
@click.option("--mutation", "mutations", multiple=True, help="Mutation slug to include. Repeat to add more.")
@click.option("--hermes-config-path", type=click.Path(exists=True, dir_okay=False), help="Optional Hermes benchmark YAML config path.")
@click.option("--task-filter", help="Optional comma-separated task filter.")
@click.option("--skip-tasks", help="Optional comma-separated task skip list.")
@click.option("--frontier-path", help="Optional frontier JSON path to update for each evaluated trial.")
@click.option("--dry-run", is_flag=True, help="Generate candidate files and print commands without executing benchmarks.")
def search_candidates_cmd(
    seed_candidate: str,
    benchmark: str,
    baseline_candidate: str,
    baseline_run: Optional[str] = None,
    baseline_from_frontier: Optional[str] = None,
    hermes_repo: Optional[str] = None,
    launcher_prefix: Optional[str] = None,
    python_executable: Optional[str] = None,
    workspace_dir: Optional[str] = None,
    archive_dir: Optional[str] = None,
    mutations: Iterable[str] = (),
    hermes_config_path: Optional[str] = None,
    task_filter: Optional[str] = None,
    skip_tasks: Optional[str] = None,
    frontier_path: Optional[str] = None,
    dry_run: bool = False,
) -> None:
    """Run a small deterministic search over generated wrapper candidates."""
    config = _build_config(
        hermes_repo,
        launcher_prefix=launcher_prefix,
        python_executable=python_executable,
    )
    request = StructuredSearchRequest(
        benchmark=benchmark,
        seed_candidate=seed_candidate,
        baseline_candidate=baseline_candidate,
        baseline_run_dir=Path(baseline_run).expanduser().resolve() if baseline_run else None,
        baseline_frontier_path=Path(baseline_from_frontier).expanduser().resolve() if baseline_from_frontier else None,
        workspace_dir=Path(workspace_dir).expanduser().resolve() if workspace_dir else None,
        archive_root=Path(archive_dir).expanduser().resolve() if archive_dir else None,
        mutation_slugs=tuple(mutations),
        hermes_config_path=Path(hermes_config_path).expanduser().resolve() if hermes_config_path else None,
        task_filter=task_filter,
        skip_tasks=skip_tasks,
        frontier_path=Path(frontier_path).expanduser().resolve() if frontier_path else None,
        python_executable=config.python_executable,
    )

    try:
        summary = run_structured_search(config, request, dry_run=dry_run)
    except (FileNotFoundError, KeyError, RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc

    overview = Table(title="Structured Search")
    overview.add_column("Metric", style="bold")
    overview.add_column("Value")
    overview.add_row("Benchmark", summary.benchmark_name)
    overview.add_row("Baseline Candidate", summary.baseline_candidate)
    overview.add_row("Baseline Source", summary.baseline_source)
    overview.add_row("Baseline Reference", summary.baseline_reference or "-")
    overview.add_row("Seed Candidate", summary.seed_candidate)
    overview.add_row("Workspace", summary.workspace_dir)
    overview.add_row("Generated Candidates", summary.generated_candidates_dir)
    overview.add_row("Trials", str(len(summary.trial_results)))
    overview.add_row("Best Mutation", summary.best_mutation_slug or "-")
    overview.add_row("Best Candidate", summary.best_candidate_name or "-")
    console.print()
    console.print(overview)

    trials_table = Table(title="Trials")
    trials_table.add_column("Mutation", style="bold")
    trials_table.add_column("Candidate")
    trials_table.add_column("Pass Rate Delta", justify="right")
    trials_table.add_column("Net Gain", justify="right")
    trials_table.add_column("Better?", justify="right")
    for trial in summary.trial_results:
        pass_rate_delta = _format_optional_delta(trial.report.pass_rate_delta) if trial.report else "-"
        net_gain = str(trial.report.net_task_gain) if trial.report else "-"
        better = "yes" if trial.report and trial.report.candidate_better else "no"
        if trial.report is None:
            better = "-"
        trials_table.add_row(
            trial.mutation_slug,
            trial.candidate_name,
            pass_rate_delta,
            net_gain,
            better,
        )
    console.print()
    console.print(trials_table)

    if dry_run:
        console.print("\n[yellow]Dry run only — generated candidates and commands were prepared.[/yellow]")


@main.command("show-run")
@click.option("--run-dir", required=True, help="Path to a Hermes Meta-Harness run dir.")
def show_run_cmd(run_dir: str) -> None:
    """Show the parsed summary for one run."""
    summary = load_run_summary(Path(run_dir))
    console.print(json.dumps(
        {
            "benchmark_name": summary.benchmark_name,
            "candidate_name": summary.candidate_name,
            "candidate_path": summary.candidate_path,
            "run_dir": str(summary.run_dir),
            "eval_metrics": summary.eval_metrics,
            "task_count": len(summary.task_results),
        },
        indent=2,
        sort_keys=True,
    ))


@main.group("playbook")
def playbook_group() -> None:
    """Initialize and run per-project agent playbooks."""


def _emit_step_results(title: str, results) -> bool:
    table = Table(title=title)
    table.add_column("Command")
    table.add_column("Status")
    ok = True
    for result in results:
        status = "ok" if result.ok else f"failed ({result.returncode})"
        if not result.ok:
            ok = False
        table.add_row(" ".join(result.command), status)
    console.print(table)
    for result in results:
        if not result.ok:
            console.print(f"[red]{result.stderr.strip()}[/red]")
    return ok


@playbook_group.command("list")
def playbook_list_cmd() -> None:
    """List available agent playbooks."""
    playbooks: list[AgentPlaybook] = list_agent_playbooks()
    if not playbooks:
        console.print("No agent playbooks found in agents/.")
        return
    table = Table(title="Agent Playbooks")
    table.add_column("Agent", style="bold")
    table.add_column("Description")
    table.add_column("Project Env Var")
    for playbook in playbooks:
        table.add_row(playbook.name, playbook.description, playbook.project_env_var)
    console.print(table)


@playbook_group.command("init")
@click.argument("agent")
def playbook_init_cmd(agent: str) -> None:
    """Run an agent playbook's setup steps only."""
    playbook = load_agent_playbook(agent)
    results = init_project(playbook)
    ok = _emit_step_results(f"Init: {playbook.name}", results)
    if not ok:
        raise click.ClickException(f"Setup failed for agent '{agent}'.")


@playbook_group.command("run")
@click.argument("agent")
@click.option(
    "--subject",
    default=None,
    help="Subject id (e.g. a ticket id) substituted into {subject_id} flow steps. "
    "The run is recorded and can later be replayed with `playbook replay`.",
)
def playbook_run_cmd(agent: str, subject: Optional[str]) -> None:
    """Run an agent playbook's complete flow: setup, then the flow steps."""
    playbook = load_agent_playbook(agent)
    if flow_requires_subject(playbook) and not subject:
        raise click.UsageError(
            f"Agent '{agent}' processes one subject at a time — pass --subject (e.g. a ticket id)."
        )
    project_path = resolve_project_path(playbook)
    setup_results = init_project(playbook, project_path=project_path)
    setup_ok = _emit_step_results(f"Setup: {playbook.name}", setup_results)
    if not setup_ok:
        raise click.ClickException(f"Setup failed for agent '{agent}'.")

    flow_results, record = run_and_record_flow(playbook, subject_id=subject, project_path=project_path)
    flow_ok = _emit_step_results(f"Flow: {playbook.name}", flow_results)
    console.print(f"\n[bold]Run recorded:[/bold] {record.run_id}"
                  + (f" (subject: {subject})" if subject else ""))
    if not flow_ok:
        raise click.ClickException(f"Flow failed for agent '{agent}'.")


@playbook_group.command("runs")
@click.argument("agent")
def playbook_runs_cmd(agent: str) -> None:
    """List recorded flow runs for an agent, most recent last."""
    playbook = load_agent_playbook(agent)
    records = RunArchive(playbook.name).load()
    if not records:
        console.print(f"No recorded runs for agent '{agent}'. Use `playbook run {agent} --subject ...`.")
        return
    table = Table(title=f"Runs: {playbook.name}")
    table.add_column("Run ID", style="bold")
    table.add_column("Subject")
    table.add_column("Started At")
    table.add_column("Status")
    for record in records:
        table.add_row(record.run_id, record.subject_id or "-", record.started_at, "ok" if record.ok else "failed")
    console.print(table)


@playbook_group.command("replay")
@click.argument("agent")
@click.argument("run_id")
def playbook_replay_cmd(agent: str, run_id: str) -> None:
    """Repeat a previously recorded flow run against the same subject."""
    playbook = load_agent_playbook(agent)
    original, flow_results, replayed = replay_run(playbook, run_id)
    console.print(f"Replaying run [bold]{run_id}[/bold] (subject: {original.subject_id or '-'})")
    flow_ok = _emit_step_results(f"Replay: {playbook.name}", flow_results)
    reproduced = replayed.ok == original.ok
    console.print(
        f"\n[bold]New run recorded:[/bold] {replayed.run_id} — "
        + ("[green]reproduced original result[/green]" if reproduced else "[yellow]result differs from original[/yellow]")
    )
    if not flow_ok:
        raise click.ClickException(f"Replay failed for agent '{agent}'.")


@main.group("qa")
def qa_group() -> None:
    """Track and triage QA review findings."""


def _qa_store(db_path: Optional[str]) -> QAFindingStore:
    return QAFindingStore(Path(db_path).expanduser().resolve() if db_path else None)


def _emit_findings_table(title: str, findings: "list[QAFinding]") -> None:
    table = Table(title=title)
    table.add_column("ID", justify="right")
    table.add_column("Project", style="bold")
    table.add_column("Route")
    table.add_column("Severity")
    table.add_column("Status")
    table.add_column("ClickUp Task")
    table.add_column("Created At")
    severity_style = {"critical": "bold red", "major": "yellow", "minor": "default"}
    for finding in findings:
        table.add_row(
            str(finding.id),
            finding.project,
            finding.route,
            f"[{severity_style.get(finding.severity, 'default')}]{finding.severity}[/]",
            finding.status,
            finding.clickup_task_id or "-",
            finding.created_at,
        )
    console.print(table)


@qa_group.command("report-issue")
@click.option("--project", required=True)
@click.option("--route", required=True, help="Route or endpoint the finding applies to.")
@click.option("--observation", required=True, help="What was observed.")
@click.option("--severity", required=True, type=click.Choice(SEVERITIES))
@click.option("--screenshot", default=None, help="Optional path to a screenshot for this finding.")
@click.option("--clickup-list-id", default=None, help="Override the ClickUp list id used for critical tickets.")
@click.option("--no-ticket", is_flag=True, help="Skip ClickUp ticket auto-creation even for critical severity.")
@click.option("--db-path", default=None, help="Override the QA findings database path.")
def qa_report_issue_cmd(
    project: str,
    route: str,
    observation: str,
    severity: str,
    screenshot: Optional[str],
    clickup_list_id: Optional[str],
    no_ticket: bool,
    db_path: Optional[str],
) -> None:
    """Report a QA finding. Critical severity auto-creates a ClickUp ticket."""
    store = _qa_store(db_path)
    try:
        finding = report_qa_issue(
            project,
            route,
            observation,
            severity,
            screenshot_path=screenshot,
            clickup_list_id=clickup_list_id,
            auto_escalate=not no_ticket,
            store=store,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    _emit_findings_table("QA Finding Reported", [finding])
    if finding.severity == "critical" and not finding.clickup_task_id and not no_ticket:
        console.print("[yellow]Finding was acknowledged, but ClickUp ticket creation failed — see logs.[/yellow]")


@qa_group.command("list-issues")
@click.option("--project", default=None)
@click.option("--severity", default=None, type=click.Choice(SEVERITIES))
@click.option("--status", default=None, type=click.Choice(STATUSES))
@click.option("--db-path", default=None)
def qa_list_issues_cmd(
    project: Optional[str], severity: Optional[str], status: Optional[str], db_path: Optional[str]
) -> None:
    """List QA findings, optionally filtered by project/severity/status."""
    findings = list_qa_issues(project=project, severity=severity, status=status, store=_qa_store(db_path))
    if not findings:
        console.print("No QA findings match the given filters.")
        return
    _emit_findings_table("QA Findings", findings)


@qa_group.command("close-issue")
@click.argument("finding_id", type=int)
@click.option("--note", required=True, help="Correction note describing the fix.")
@click.option("--db-path", default=None)
def qa_close_issue_cmd(finding_id: int, note: str, db_path: Optional[str]) -> None:
    """Close a QA finding with a correction note."""
    try:
        finding = close_qa_issue(finding_id, note, store=_qa_store(db_path))
    except QAFindingNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    _emit_findings_table("QA Finding Closed", [finding])


def _emit_review(title: str, review, finding, record) -> None:
    table = Table(title=title)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Run ID", record.run_id)
    table.add_row("Ticket", f"{review.ticket_id} — {review.ticket_name}")
    table.add_row("Severity", review.severity)
    table.add_row("Observation", review.observation)
    if finding is not None:
        table.add_row("Persisted", f"[green]yes — finding #{finding.id}[/green]")
    else:
        table.add_row("Persisted", "[yellow]no (dry-run)[/yellow]")
    console.print(table)


@qa_group.command("review-ticket")
@click.option("--ticket-id", required=True, help="ClickUp ticket id to review.")
@click.option("--project", required=True, help="Project name to file the finding under.")
@click.option("--persist", is_flag=True, help="Report the review as a real finding (default: dry-run only).")
@click.option("--clickup-list-id", default=None, help="Override the ClickUp list id used for critical tickets.")
@click.option("--db-path", default=None)
def qa_review_ticket_cmd(
    ticket_id: str, project: str, persist: bool, clickup_list_id: Optional[str], db_path: Optional[str]
) -> None:
    """Review a ClickUp ticket as a QA flow: fetch it, analyze it, and
    (optionally) report a finding. Dry-run by default."""
    try:
        review, finding, record = review_and_record(
            ticket_id, project=project, clickup_list_id=clickup_list_id,
            persist=persist, store=_qa_store(db_path),
        )
    except (QAFlowError, ClickUpReadError) as exc:
        raise click.ClickException(str(exc)) from exc
    _emit_review("QA Ticket Review", review, finding, record)


@qa_group.command("review-runs")
def qa_review_runs_cmd() -> None:
    """List recorded QA ticket reviews, most recent last."""
    records = RunArchive("qa_ticket_review").load()
    if not records:
        console.print("No recorded QA ticket reviews. Use `qa review-ticket --ticket-id ... --project ...`.")
        return
    table = Table(title="QA Ticket Reviews")
    table.add_column("Run ID", style="bold")
    table.add_column("Ticket")
    table.add_column("Started At")
    table.add_column("Status")
    for record in records:
        table.add_row(record.run_id, record.subject_id or "-", record.started_at, "ok" if record.ok else "failed")
    console.print(table)


@qa_group.command("replay-review")
@click.argument("run_id")
@click.option("--persist", is_flag=True, help="Report the replayed review as a real finding (default: dry-run only).")
@click.option("--clickup-list-id", default=None, help="Override the ClickUp list id used for critical tickets.")
@click.option("--db-path", default=None)
def qa_replay_review_cmd(run_id: str, persist: bool, clickup_list_id: Optional[str], db_path: Optional[str]) -> None:
    """Repeat a previously recorded ticket review against the same ticket."""
    try:
        original, review, finding, record = replay_qa_review(
            run_id, persist=persist, clickup_list_id=clickup_list_id, store=_qa_store(db_path),
        )
    except (QAFlowError, ClickUpReadError, FileNotFoundError) as exc:
        raise click.ClickException(str(exc)) from exc
    console.print(f"Replaying review [bold]{run_id}[/bold] (ticket: {original.subject_id})")
    _emit_review("Replayed Review", review, finding, record)


@main.command("ui")
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8877, show_default=True, type=int)
def ui_cmd(host: str, port: int) -> None:
    """Launch the local Meta-Harness web UI (FastAPI + static frontend)."""
    import uvicorn

    uvicorn.run("meta_harness.webapp.app:app", host=host, port=port)


@main.command("mcp-server")
def mcp_server_cmd() -> None:
    """Launch the Meta-Harness MCP server over stdio."""
    from meta_harness.mcp_server.server import main as run_mcp_server

    run_mcp_server()


@main.group("tickets")
def tickets_group() -> None:
    """Generate proposed tickets from a document and create them in ClickUp."""


def _proposed_ticket_to_dict(ticket: ProposedTicket) -> dict:
    return {
        "title": ticket.title,
        "description": ticket.description,
        "acceptance_criteria": ticket.acceptance_criteria,
        "priority": ticket.priority,
        "category": ticket.category,
    }


@tickets_group.command("generate")
@click.option("--file", "file_path", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--json-output", default=None, help="Optional path to write proposed tickets as JSON.")
@click.option(
    "--start-number", default=1, show_default=True, type=int,
    help="First number in the TAS sequence, to continue an existing backlog.",
)
def tickets_generate_cmd(
    file_path: str,
    json_output: Optional[str],
    start_number: int,
) -> None:
    """Analyze a document and propose tickets (not yet created anywhere).

    Every ticket is named "TAS-NN | <title>" in the order proposed. Tickets
    are still classified (backend/frontend/deployment/mundane) as a label for
    humans scanning the backlog, but the classification no longer changes the
    identifier.
    """
    path = Path(file_path)
    try:
        tickets, warnings = generate_tickets_from_file(path.name, path.read_bytes())
    except (TicketExtractionError, ClaudeNotFoundError, TicketGenerationError, TicketParseError) as exc:
        raise click.ClickException(str(exc)) from exc

    tickets = apply_ticket_numbering(tickets, start_number)

    table = Table(title="Proposed Tickets")
    table.add_column("Title", style="bold")
    table.add_column("Category")
    table.add_column("Priority")
    table.add_column("Acceptance Criteria")
    for ticket in tickets:
        table.add_row(ticket.title, ticket.category, ticket.priority, "; ".join(ticket.acceptance_criteria))
    console.print(table)
    for warning in warnings:
        console.print(f"[yellow]{warning}[/yellow]")

    if json_output:
        _write_json(Path(json_output).expanduser().resolve(), [_proposed_ticket_to_dict(t) for t in tickets])


@tickets_group.command("create-all")
@click.option("--from-json", "from_json_path", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--list-id", default=None, help="Defaults to CLICKUP_LIST_ID from .env.")
def tickets_create_all_cmd(from_json_path: str, list_id: Optional[str]) -> None:
    """Create every ticket in a proposed-tickets JSON file in ClickUp."""
    raw = json.loads(Path(from_json_path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise click.ClickException(f"Expected a JSON array of tickets in {from_json_path}")

    table = Table(title="Ticket Creation Results")
    table.add_column("Title", style="bold")
    table.add_column("Status")
    ok_count = 0
    for item in raw:
        title = item.get("title", "(untitled)")
        description = item.get("description", "")
        criteria = item.get("acceptance_criteria", [])
        lines = [description, "", "Acceptance Criteria:"]
        lines += [f"- {c}" for c in criteria] or ["- (none specified)"]
        try:
            task_id = create_clickup_ticket(
                title, "\n".join(lines), list_id=list_id, priority=item.get("priority")
            )
        except (ClickUpTicketError, ValueError) as exc:
            table.add_row(title, f"[red]failed: {exc}[/red]")
        else:
            table.add_row(title, f"[green]created: {task_id}[/green]")
            ok_count += 1
    console.print(table)
    console.print(f"\n[bold]{ok_count}/{len(raw)} tickets created.[/bold]")


@main.group("clickup")
def clickup_group() -> None:
    """Read and write ClickUp teams, lists and tasks directly."""


def _echo_json(payload) -> None:
    click.echo(json.dumps(payload, indent=2))


def _tracker_command(func):
    """Report tracker failures as a one-line CLI error, not a traceback.

    A missing token or a rejected request is an expected outcome here, so it
    reads like `meta-harness: <what went wrong>` the way the rest of the CLI
    reports RuntimeErrors.
    """

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except (
            ClickUpReadError,
            ClickUpTicketError,
            LinearIssueError,
            LinearReadError,
            ValueError,
        ) as exc:
            raise _clickify_runtime_error(exc) from exc

    return wrapper


@clickup_group.command("teams")
@_tracker_command
def clickup_teams_cmd() -> None:
    """List every ClickUp team the token can see."""
    _echo_json(list_clickup_teams())


@clickup_group.command("spaces")
@click.option("--team-id", default=None, help="Defaults to CLICKUP_TEAM_ID from .env.")
@_tracker_command
def clickup_spaces_cmd(team_id: Optional[str]) -> None:
    """List the spaces in a team."""
    team_id = team_id or ClickUpConfig.from_env().clickup_team_id
    if not team_id:
        raise click.UsageError("Provide --team-id or set CLICKUP_TEAM_ID in .env.")
    _echo_json(list_clickup_spaces(team_id))


@clickup_group.command("folders")
@click.option("--space-id", required=True)
@_tracker_command
def clickup_folders_cmd(space_id: str) -> None:
    """List the folders in a space."""
    _echo_json(list_clickup_folders(space_id))


@clickup_group.command("lists")
@click.option("--space-id", default=None)
@click.option("--folder-id", default=None)
@_tracker_command
def clickup_lists_cmd(space_id: Optional[str], folder_id: Optional[str]) -> None:
    """List the lists in a space (folderless) or a folder."""
    _echo_json(list_clickup_lists(space_id, folder_id=folder_id))


@clickup_group.command("get-task")
@click.option("--task-id", required=True)
@_tracker_command
def clickup_get_task_cmd(task_id: str) -> None:
    """Fetch a single task by id."""
    _echo_json(get_clickup_task(task_id))


@clickup_group.command("tasks")
@click.option("--list-id", required=True)
@_tracker_command
def clickup_tasks_cmd(list_id: str) -> None:
    """List every task in a list, subtasks included."""
    _echo_json(list_clickup_tasks(list_id))


@clickup_group.command("create-task")
@click.option("--list-id", default=None, help="Defaults to CLICKUP_LIST_ID from .env.")
@click.option("--name", required=True)
@click.option("--description", default=None)
@click.option(
    "--priority",
    type=click.Choice(list(CLICKUP_PRIORITY_WORDS)),
    default=None,
    help="ClickUp priority: urgent, high, normal, or low.",
)
@click.option("--assignees", default=None, help="Comma-separated ClickUp user IDs.")
@click.option("--due-date", default=None, type=int, help="Due date as a Unix timestamp in milliseconds.")
@click.option("--parent", default=None, help="ClickUp task id to nest this task under as a subtask.")
def clickup_create_task_cmd(
    list_id: Optional[str],
    name: str,
    description: Optional[str],
    priority: Optional[str],
    assignees: Optional[str],
    due_date: Optional[int],
    parent: Optional[str],
) -> None:
    """Create a task."""
    task_id = create_clickup_ticket(
        name,
        description or "",
        list_id=list_id,
        priority=priority,
        assignees=[int(a) for a in assignees.split(",") if a.strip()] if assignees else None,
        due_date_ms=due_date,
        parent=parent,
    )
    _echo_json({"id": task_id})


@clickup_group.command("set-status")
@click.option("--task-id", required=True)
@click.option("--status", required=True, help='A status name valid for the task\'s list, e.g. "done".')
@_tracker_command
def clickup_set_status_cmd(task_id: str, status: str) -> None:
    """Move a task to a different status."""
    _echo_json(update_clickup_task_status(task_id, status))


@clickup_group.command("update-task")
@click.option("--task-id", required=True)
@click.option("--name", default=None, help="New task name. Left unchanged when omitted.")
@click.option("--description", default=None, help="New task description. Left unchanged when omitted.")
@_tracker_command
def clickup_update_task_cmd(task_id: str, name: Optional[str], description: Optional[str]) -> None:
    """Edit a task's name and/or description."""
    if name is None and description is None:
        raise click.UsageError("Provide --name and/or --description.")
    _echo_json(update_clickup_task(task_id, name=name, description=description))


@main.group("linear")
def linear_group() -> None:
    """Read and write Linear teams, issues and workflow states directly."""


@linear_group.command("viewer")
@_tracker_command
def linear_viewer_cmd() -> None:
    """Show the authenticated user and organization."""
    _echo_json(get_linear_viewer())


@linear_group.command("teams")
@_tracker_command
def linear_teams_cmd() -> None:
    """List every Linear team."""
    _echo_json(list_linear_teams())


@linear_group.command("states")
@click.option("--team-id", required=True)
@_tracker_command
def linear_states_cmd(team_id: str) -> None:
    """List a team's workflow states."""
    _echo_json(list_linear_states(team_id))


@linear_group.command("members")
@click.option("--team-id", required=True)
@_tracker_command
def linear_members_cmd(team_id: str) -> None:
    """List a team's members."""
    _echo_json(list_linear_members(team_id))


@linear_group.command("projects")
@click.option("--team-id", required=True)
@_tracker_command
def linear_projects_cmd(team_id: str) -> None:
    """List a team's projects."""
    _echo_json(list_linear_projects(team_id))


@linear_group.command("issues")
@click.option("--team-id", required=True)
@click.option(
    "--limit",
    default=None,
    type=int,
    help="Maximum issues to return. Omit to fetch every issue on the team.",
)
def linear_issues_cmd(team_id: str, limit: Optional[int]) -> None:
    """List a team's issues."""
    _echo_json(list_linear_issues(team_id, limit=limit))


@linear_group.command("get-issue")
@click.option("--issue-id", required=True)
@_tracker_command
def linear_get_issue_cmd(issue_id: str) -> None:
    """Fetch a single issue by id."""
    _echo_json(get_linear_issue(issue_id))


@linear_group.command("create-issue")
@click.option("--team-id", required=True)
@click.option("--title", required=True)
@click.option("--description", default=None)
@click.option(
    "--priority",
    type=click.Choice(list(LINEAR_PRIORITY_WORDS)),
    default=None,
    help="Linear priority: urgent, high, normal, or low.",
)
@click.option("--assignee-id", default=None, help="Linear user id to assign the issue to.")
@click.option("--due-date", default=None, help="Due date as an ISO date, e.g. 2026-08-24.")
@click.option("--project-id", default=None)
@click.option("--parent-id", default=None, help="Linear issue id to nest this issue under as a sub-issue.")
def linear_create_issue_cmd(
    team_id: str,
    title: str,
    description: Optional[str],
    priority: Optional[str],
    assignee_id: Optional[str],
    due_date: Optional[str],
    project_id: Optional[str],
    parent_id: Optional[str],
) -> None:
    """Create an issue."""
    issue_id = create_linear_issue(
        team_id,
        title,
        description or "",
        priority=priority,
        assignee_id=assignee_id,
        due_date=due_date,
        project_id=project_id,
        parent_id=parent_id,
    )
    _echo_json({"id": issue_id})


@linear_group.command("set-state")
@click.option("--issue-id", required=True)
@click.option("--state-id", required=True, help="A workflow state id valid for the issue's team.")
@_tracker_command
def linear_set_state_cmd(issue_id: str, state_id: str) -> None:
    """Move an issue to a different workflow state."""
    _echo_json(update_linear_issue_state(issue_id, state_id))


@linear_group.command("update-issue")
@click.option("--issue-id", required=True)
@click.option("--title", default=None, help="New issue title. Left unchanged when omitted.")
@click.option("--description", default=None, help="New issue description. Left unchanged when omitted.")
@_tracker_command
def linear_update_issue_cmd(issue_id: str, title: Optional[str], description: Optional[str]) -> None:
    """Edit an issue's title and/or description."""
    if title is None and description is None:
        raise click.UsageError("Provide --title and/or --description.")
    _echo_json(update_linear_issue(issue_id, title=title, description=description))


@main.group("index")
def index_group() -> None:
    """Index repositories so the harness can retrieve real context from source."""


def _resolve_repo_name(repo: str, name: Optional[str]) -> str:
    return name or Path(repo).expanduser().resolve().name


@index_group.command("build")
@click.option("--repo", required=True, type=click.Path(exists=True, file_okay=False), help="Path to the repository to index.")
@click.option("--name", default=None, help="Name to index it under. Defaults to the directory name.")
@click.option("--rebuild", is_flag=True, help="Discard the existing index for this repo before indexing.")
@click.option(
    "--full/--skeleton",
    "full",
    default=False,
    show_default=True,
    help="Index whole file bodies instead of just what each file declares. "
         "Skeleton is ~15% of the text and the real code is still read from disk on retrieval.",
)
@click.option("--db-path", type=click.Path(dir_okay=False), help="Override the index database location.")
def index_build_cmd(
    repo: str, name: Optional[str], rebuild: bool, full: bool, db_path: Optional[str]
) -> None:
    """Embed a repository's source into the index.

    Indexes declarations by default — signatures, types and their doc
    comments — which is a fraction of the text and enough to find things by.
    The real code is read from the file when a result is returned.

    Incremental: files whose contents are unchanged since the last run are
    skipped without being re-embedded.
    """
    repo_name = _resolve_repo_name(repo, name)
    store = VectorStore(Path(db_path) if db_path else None)
    try:
        result = index_repository(
            Path(repo),
            embedder=resolve_embedder(),
            store=store,
            repo_name=repo_name,
            rebuild=rebuild,
            mode="full" if full else "skeleton",
            on_step=lambda message: console.print(f"[dim]{message}[/dim]"),
        )
    except (EmbeddingError, VectorStoreError, NotADirectoryError, ValueError) as exc:
        raise _clickify_runtime_error(exc) from exc
    finally:
        store.close()

    table = Table(title=f"Indexed {result.repo}")
    table.add_column("Metric", style="bold")
    table.add_column("Count", justify="right")
    table.add_row("Files scanned", str(result.files_scanned))
    table.add_row("Files embedded", str(result.files_embedded))
    table.add_row("Chunks embedded", str(result.chunks_embedded))
    table.add_row("Unchanged (skipped)", str(result.files_skipped_unchanged))
    table.add_row("Removed", str(result.files_removed))
    console.print(table)

    for message in result.errors:
        console.print(f"[red]{message}[/red]")
    if result.errors:
        raise click.ClickException(f"{len(result.errors)} file(s) failed to index.")


@index_group.command("search")
@click.argument("query")
@click.option("--repo", required=True, help="Name the repository was indexed under.")
@click.option("--limit", default=10, show_default=True, type=int, help="Maximum chunks to return.")
@click.option("--min-score", default=0.0, show_default=True, type=float, help="Drop hits below this cosine score.")
@click.option("--full", is_flag=True, help="Print each chunk's full text rather than a preview.")
@click.option(
    "--lexical/--no-lexical",
    default=None,
    help="Force the exact-symbol pass on or off. Default: on when the query looks like an identifier.",
)
@click.option("--verify", is_flag=True, help="Re-read each result from disk and report whether it still matches.")
@click.option("--db-path", type=click.Path(dir_okay=False), help="Override the index database location.")
def index_search_cmd(
    query: str, repo: str, limit: int, min_score: float, full: bool,
    lexical: Optional[bool], verify: bool, db_path: Optional[str]
) -> None:
    """Retrieve the source most relevant to a query.

    Combines semantic retrieval with an exact-symbol pass, because embeddings
    are weakest at precisely the thing a symbol name is: an exact string.
    """
    store = VectorStore(Path(db_path) if db_path else None)
    try:
        hits = hybrid_search(
            query, repo=repo, embedder=resolve_embedder(), store=store,
            limit=limit, min_score=min_score, lexical=lexical, verify=verify,
        )
    except (EmbeddingError, VectorStoreError, ValueError) as exc:
        raise _clickify_runtime_error(exc) from exc
    finally:
        store.close()

    if not hits:
        console.print("[yellow]No matches.[/yellow]")
        return

    marks = {"verified": "[green]verified[/green]", "drifted": "[yellow]drifted[/yellow]",
             "missing": "[red]missing[/red]"}
    for hit in hits:
        tags = [hit.language, hit.retrieval]
        if hit.verification != "unchecked":
            tags.append(marks.get(hit.verification, hit.verification))
        console.print(f"\n[bold]{hit.location}[/bold]  [dim]({', '.join(tags)}, score {hit.score:.3f})[/dim]")
        body = hit.text if full else "\n".join(hit.text.splitlines()[:12])
        console.print(body)


@index_group.command("status")
@click.option("--db-path", type=click.Path(dir_okay=False), help="Override the index database location.")
def index_status_cmd(db_path: Optional[str]) -> None:
    """Show what is currently indexed."""
    store = VectorStore(Path(db_path) if db_path else None)
    try:
        repos = store.repos()
    finally:
        store.close()

    if not repos:
        console.print("[yellow]Nothing indexed yet. Run: meta-harness index build --repo <path>[/yellow]")
        return

    table = Table(title="Indexed repositories")
    table.add_column("Repository", style="bold")
    table.add_column("Files", justify="right")
    table.add_column("Chunks", justify="right")
    for repo_name, files, chunks in repos:
        table.add_row(repo_name, str(files), str(chunks))
    console.print(table)


@index_group.command("drop")
@click.option("--repo", required=True, help="Name of the indexed repository to remove.")
@click.option("--db-path", type=click.Path(dir_okay=False), help="Override the index database location.")
def index_drop_cmd(repo: str, db_path: Optional[str]) -> None:
    """Remove a repository from the index."""
    store = VectorStore(Path(db_path) if db_path else None)
    try:
        store.clear_repo(repo)
    finally:
        store.close()
    console.print(f"[green]Dropped '{repo}' from the index.[/green]")


@main.command("doctor")
@click.option("--strict", is_flag=True, help="Exit non-zero on degraded checks too, not just blocking ones.")
def doctor_cmd(strict: bool) -> None:
    """Check that everything this app needs is present and configured."""
    checks = run_all()

    symbols = {"ok": "[green]OK[/green]", "degraded": "[yellow]WARN[/yellow]", "missing": "[red]FAIL[/red]"}
    table = Table(title="Preflight")
    table.add_column("")
    table.add_column("Check", style="bold")
    table.add_column("Detail")
    for check in checks:
        table.add_row(symbols.get(check.status, check.status), check.name, check.detail)
    console.print(table)

    needs_action = [check for check in checks if check.status != "ok"]
    if needs_action:
        console.print("\n[bold]To fix:[/bold]")
        for check in needs_action:
            if check.fix:
                console.print(f"  [dim]{check.name}:[/dim] {check.fix}")

    blocking = [check for check in checks if check.blocking]
    if blocking:
        raise click.ClickException(
            f"{len(blocking)} blocking problem(s). The app will not start until these are fixed."
        )
    if strict and needs_action:
        raise click.ClickException(f"{len(needs_action)} check(s) degraded (--strict).")
    console.print("\n[green]Ready.[/green]")


@main.group("rework")
def rework_group() -> None:
    """Measure the ticket-name matcher behind the Linear rework flow."""


@rework_group.command("benchmark")
@click.option("--cases", required=True, type=click.Path(exists=True, dir_okay=False),
              help="Hand-resolved case set (JSON).")
@click.option("--archive-dir", required=True, type=click.Path(file_okay=False),
              help="Where to write the run directory.")
@click.option("--candidate-name", default="default-thresholds", help="Name this run is reported under.")
@click.option("--strong-match", type=float, default=None, help="Score at or above which a match is accepted.")
@click.option("--min-candidate", type=float, default=None, help="Score below which a candidate is discarded.")
@click.option("--ambiguity-margin", type=float, default=None,
              help="Gap the top two candidates must exceed to be considered separated.")
def rework_benchmark_cmd(
    cases: str,
    archive_dir: str,
    candidate_name: str,
    strong_match: Optional[float],
    min_candidate: Optional[float],
    ambiguity_margin: Optional[float],
) -> None:
    """Score the matcher and write a run the outer loop can compare.

    The run directory is the same shape `compare-runs` and `show-frontier`
    already read, so two threshold settings are compared with those rather
    than by eye.
    """
    from meta_harness.rework.benchmark import CaseSet, run_benchmark
    from meta_harness.rework.matching import DEFAULT_THRESHOLDS, Thresholds

    thresholds = Thresholds(
        strong_match=DEFAULT_THRESHOLDS.strong_match if strong_match is None else strong_match,
        min_candidate=DEFAULT_THRESHOLDS.min_candidate if min_candidate is None else min_candidate,
        ambiguity_margin=(
            DEFAULT_THRESHOLDS.ambiguity_margin if ambiguity_margin is None else ambiguity_margin
        ),
    )
    try:
        thresholds.validate()
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc

    case_set = CaseSet.load(Path(cases))
    result = run_benchmark(
        case_set,
        archive_root=Path(archive_dir),
        thresholds=thresholds,
        candidate_name=candidate_name,
    )
    click.echo(result.summary_line())
    if result.false_matches:
        # Called out separately: this is the failure that gets acted on.
        click.echo("")
        click.echo("False matches (a name resolved to the wrong issue):")
        for outcome in result.false_matches:
            click.echo(f"  {outcome.task_name}: expected {outcome.expected or '(none)'}, got {outcome.actual}")
    click.echo("")
    click.echo(f"Run written to {result.run_dir}")
@main.group("source")
def source_group() -> None:
    """Register the repositories the harness may draw context from."""


def _source_store(db_path: Optional[str]) -> VectorStore:
    return VectorStore(Path(db_path) if db_path else None)


@source_group.command("add")
@click.option("--repo", required=True, type=click.Path(exists=True, file_okay=False), help="Path to the repository.")
@click.option("--name", default=None, help="Name to register it under. Defaults to the directory name.")
@click.option("--index/--no-index", "do_index", default=True, show_default=True, help="Index it immediately.")
@click.option("--db-path", type=click.Path(dir_okay=False))
def source_add_cmd(repo: str, name: Optional[str], do_index: bool, db_path: Optional[str]) -> None:
    """Register a repository as a trusted context source.

    Registering without indexing declares the source but retrieves nothing
    from it — the index is what makes it usable.
    """
    repo_name = _resolve_repo_name(repo, name)
    store = _source_store(db_path)
    try:
        source = store.sources.register(repo_name, Path(repo))
        console.print(
            f"[green]Registered[/green] '{source.name}' -> {source.root}"
            + (f"  [dim](at {source.revision[:8]})[/dim]" if source.revision else "")
        )
        if do_index:
            result = index_repository(
                Path(repo), embedder=resolve_embedder(), store=store, repo_name=repo_name,
                on_step=lambda message: console.print(f"[dim]{message}[/dim]"),
            )
            console.print(f"[green]Indexed[/green] {result.summary()}")
    except (SourceError, EmbeddingError, VectorStoreError, NotADirectoryError) as exc:
        raise _clickify_runtime_error(exc) from exc
    finally:
        store.close()


@source_group.command("list")
@click.option("--db-path", type=click.Path(dir_okay=False))
def source_list_cmd(db_path: Optional[str]) -> None:
    """Show every registered source and whether its index is current."""
    store = _source_store(db_path)
    try:
        sources = store.sources.list_sources()
        rows = []
        for source in sources:
            try:
                rows.append((source, source_status(store, source.name)))
            except SourceError:
                rows.append((source, None))
    finally:
        store.close()

    if not rows:
        console.print("[yellow]No sources registered. Add one: meta-harness source add --repo <path>[/yellow]")
        return

    table = Table(title="Context sources")
    table.add_column("Name", style="bold")
    table.add_column("Root")
    table.add_column("Revision")
    table.add_column("Status")
    for source, status in rows:
        table.add_row(
            source.name,
            str(source.root),
            (source.revision or "-")[:8],
            status.summary() if status else "unknown",
        )
    console.print(table)


@source_group.command("verify")
@click.option("--name", default=None, help="Source to verify. Defaults to all of them.")
@click.option("--db-path", type=click.Path(dir_okay=False))
def source_verify_cmd(name: Optional[str], db_path: Optional[str]) -> None:
    """Check each source's index against what is on disk.

    An index is a copy, and a copy goes stale without looking wrong. This is
    how you find out before the context is used rather than after.
    """
    store = _source_store(db_path)
    try:
        names = [name] if name else [source.name for source in store.sources.list_sources()]
        if not names:
            console.print("[yellow]No sources registered.[/yellow]")
            return
        stale = 0
        for source_name in names:
            try:
                status = source_status(store, source_name)
            except SourceError as exc:
                raise _clickify_runtime_error(exc) from exc
            marker = "[green]OK[/green]" if status.current and status.source.exists else "[yellow]STALE[/yellow]"
            if not (status.current and status.source.exists):
                stale += 1
            console.print(f"{marker}  [bold]{source_name}[/bold]  {status.summary()}")
    finally:
        store.close()

    if stale:
        console.print(
            f"\n[dim]Re-index with: meta-harness index build --repo <path>[/dim]"
        )


@source_group.command("remove")
@click.option("--name", required=True, help="Source to remove.")
@click.option("--keep-index", is_flag=True, help="Deregister but leave the indexed chunks in place.")
@click.option("--db-path", type=click.Path(dir_okay=False))
def source_remove_cmd(name: str, keep_index: bool, db_path: Optional[str]) -> None:
    """Remove a source. Its chunks go too, unless --keep-index."""
    store = _source_store(db_path)
    try:
        if not store.sources.remove(name):
            raise click.ClickException(f"'{name}' is not a registered source.")
        if not keep_index:
            store.clear_repo(name)
    finally:
        store.close()
    console.print(f"[green]Removed[/green] '{name}'" + (" (index kept)" if keep_index else ""))
