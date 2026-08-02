"""Endpoints for rebuilding a module's tickets under one parent.

Split the way the domain is: `/preview` only reads, `/apply` only writes. The
UI is expected to call preview as many times as it takes to get the list right,
so nothing on that path may have a side effect.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from meta_harness.linear_bridge import LinearIssueError, LinearReadError
from meta_harness.rework.commands import execute_rework_plan, undo_rework_run
from meta_harness.rework.models import ReworkItem, ReworkPlan
from meta_harness.rework.queries import (
    find_parent_candidates,
    list_undoable_runs,
    list_parent_options,
    preview_rework,
)
from meta_harness.rework.reasoning import claude_reasoner
from meta_harness.webapp import progress
from meta_harness.webapp.schemas import ReworkApplyIn, ReworkPreviewIn, ReworkUndoIn

router = APIRouter()


def _on_step(token):
    if not token:
        return None
    progress.start(token)
    return lambda message: progress.push(token, message)


@router.get("/parents")
def get_parents(team_id: str = Query(...), q: str = Query("")) -> list:
    """Possible parent issues.

    With no `q` this is the whole team, so the UI can offer a list to browse
    rather than a box that stays empty until the right word is guessed.
    """
    try:
        candidates = find_parent_candidates(team_id, q) if q.strip() else list_parent_options(team_id)
    except (LinearReadError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return [
        {
            "issue_id": option.issue_id,
            "identifier": option.identifier,
            "title": option.title,
            "state_name": option.state_name,
        }
        for option in candidates
    ]


@router.post("/preview")
def post_preview(body: ReworkPreviewIn) -> dict:
    """Resolve pasted names to real issues. Changes nothing."""
    try:
        preview = preview_rework(
            body.team_id,
            body.names,
            reasoner=claude_reasoner if body.use_reasoning else None,
            on_step=_on_step(body.progress_token),
        )
    except (LinearReadError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return preview.to_dict()


@router.post("/apply")
def post_apply(body: ReworkApplyIn) -> dict:
    """Create the reworked sub-issues and cancel the originals."""
    plan = ReworkPlan(
        team_id=body.team_id,
        parent_issue_id=body.parent_issue_id,
        items=[
            ReworkItem(
                issue_id=item.issue_id,
                identifier=item.identifier,
                original_title=item.original_title,
            )
            for item in body.items
        ],
        cancelled_state_id=body.cancelled_state_id,
        cancel_originals=body.cancel_originals,
    )
    try:
        plan.validate()
    except ValueError as exc:
        # A bad plan is the caller's mistake, not an upstream failure.
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        report = execute_rework_plan(plan, on_step=_on_step(body.progress_token))
    except (LinearIssueError, LinearReadError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return report.to_dict()


@router.get("/runs")
def get_runs(team_id: str = Query("")) -> list:
    """Completed runs that can still be undone, newest first."""
    return list_undoable_runs(team_id=team_id or None)


@router.post("/undo")
def post_undo(body: ReworkUndoIn) -> dict:
    """Reverse a run: originals back to their column, replacements deleted."""
    try:
        report = undo_rework_run(body.run_id, on_step=_on_step(body.progress_token))
    except ValueError as exc:
        # An unknown or already-undone run is the caller's mistake, not an outage.
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (LinearIssueError, LinearReadError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return report.to_dict()
