"""Rebuild a module's tickets as a formatted hierarchy under one parent.

A module's tickets accumulate across many hands and many months: some have no
acceptance criteria, some are a single sentence, some are empty. Fixing them
one at a time is the reason it never gets done.

This takes a pasted list of ticket names — as sent, decoration and all —
resolves each to a real Linear issue, rewrites it into the house format, files
it as a sub-issue of a parent you choose, and moves the original to cancelled
so nothing is lost.

Read and write are split (CQRS): `queries` resolves and previews and can never
change anything, `commands` is the only module that writes. Preview is
therefore safe to run as many times as it takes to get the list right.
"""

from meta_harness.rework.benchmark import CaseSet, MatchCase, run_benchmark, sweep
from meta_harness.rework.commands import execute_rework_plan, undo_rework_run
from meta_harness.rework.history import ReworkHistoryStore
from meta_harness.rework.matching import (
    DEFAULT_THRESHOLDS,
    IssueMatch,
    NameResolution,
    ParsedName,
    Thresholds,
    STATUS_AMBIGUOUS,
    STATUS_MATCHED,
    STATUS_UNMATCHED,
    parse_pasted_names,
    resolve_names,
    score_title,
)
from meta_harness.rework.models import (
    ParentOption,
    ReworkItem,
    ReworkOutcome,
    ReworkPlan,
    ReworkPreview,
    ReworkReport,
    UndoOutcome,
    UndoReport,
    WorkflowState,
)
from meta_harness.rework.queries import (
    find_cancelled_state,
    list_undoable_runs,
    find_parent_candidates,
    get_parent_option,
    list_workflow_states,
    preview_rework,
)
from meta_harness.rework.reasoning import claude_reasoner

__all__ = [
    "CaseSet",
    "DEFAULT_THRESHOLDS",
    "IssueMatch",
    "MatchCase",
    "NameResolution",
    "ParentOption",
    "ParsedName",
    "Thresholds",
    "ReworkItem",
    "ReworkOutcome",
    "ReworkPlan",
    "ReworkPreview",
    "ReworkHistoryStore",
    "ReworkReport",
    "UndoOutcome",
    "UndoReport",
    "STATUS_AMBIGUOUS",
    "STATUS_MATCHED",
    "STATUS_UNMATCHED",
    "WorkflowState",
    "claude_reasoner",
    "execute_rework_plan",
    "find_cancelled_state",
    "find_parent_candidates",
    "get_parent_option",
    "list_workflow_states",
    "parse_pasted_names",
    "preview_rework",
    "list_undoable_runs",
    "resolve_names",
    "undo_rework_run",
    "run_benchmark",
    "score_title",
    "sweep",
]
