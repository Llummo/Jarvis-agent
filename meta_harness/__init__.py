"""Standalone outer-loop Meta-Harness orchestration for Hermes."""

from meta_harness.archive_reader import load_run_summary
from meta_harness.baseline import BaselineSelection, resolve_baseline_selection
from meta_harness.benchmark_runner import run_benchmark
from meta_harness.candidate_registry import list_builtin_candidates, resolve_candidate_path
from meta_harness.comparison import build_comparison_report, compare_runs
from meta_harness.config import MetaHarnessConfig
from meta_harness.frontier import FrontierStore
from meta_harness.hermes_compat import inspect_hermes_compatibility
from meta_harness.playbook import (
    AgentPlaybook,
    init_project,
    list_agent_playbooks,
    load_agent_playbook,
    resolve_project_path,
    run_complete,
    run_flow,
)
from meta_harness.search import StructuredSearchRequest, run_structured_search

__all__ = [
    "AgentPlaybook",
    "BaselineSelection",
    "FrontierStore",
    "MetaHarnessConfig",
    "StructuredSearchRequest",
    "build_comparison_report",
    "compare_runs",
    "init_project",
    "list_agent_playbooks",
    "list_builtin_candidates",
    "load_agent_playbook",
    "load_run_summary",
    "resolve_baseline_selection",
    "resolve_candidate_path",
    "resolve_project_path",
    "run_structured_search",
    "run_benchmark",
    "run_complete",
    "run_flow",
    "inspect_hermes_compatibility",
]
