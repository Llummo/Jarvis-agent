"""Live Linear browser endpoints (read-only) plus issue creation and
state moves — the Linear-side counterpart to routes_clickup.py."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from meta_harness.linear_bridge import (
    LinearIssueError,
    create_linear_issue,
    get_linear_issue,
    list_linear_issues,
    list_linear_members,
    list_linear_projects,
    list_linear_states,
    list_linear_teams,
    update_linear_issue_state,
)
from meta_harness.ticket_format import format_linear_description
from meta_harness.webapp.deps import get_linear_project_path
from meta_harness.webapp.schemas import (
    CreateLinearIssuesIn,
    CreateLinearIssuesOut,
    LinearIssueCreateResult,
    SetLinearStateIn,
)

router = APIRouter()


@router.get("/teams")
def get_teams(project_path: Optional[Path] = Depends(get_linear_project_path)) -> list:
    return list_linear_teams(project_path=project_path)


@router.get("/states")
def get_states(
    team_id: str = Query(...), project_path: Optional[Path] = Depends(get_linear_project_path)
) -> list:
    return list_linear_states(team_id, project_path=project_path)


@router.get("/members")
def get_members(
    team_id: str = Query(...), project_path: Optional[Path] = Depends(get_linear_project_path)
) -> list:
    return list_linear_members(team_id, project_path=project_path)


@router.get("/projects")
def get_projects(
    team_id: str = Query(...), project_path: Optional[Path] = Depends(get_linear_project_path)
) -> list:
    return list_linear_projects(team_id, project_path=project_path)


@router.get("/issues")
def get_issues(
    team_id: str = Query(...),
    limit: Optional[int] = None,
    project_path: Optional[Path] = Depends(get_linear_project_path),
) -> list:
    """Defaults to every issue on the team — a fixed page size here would
    silently hide work from every dropdown that feeds off this."""
    return list_linear_issues(team_id, limit=limit, project_path=project_path)


@router.get("/issues/{issue_id}")
def get_issue(
    issue_id: str, project_path: Optional[Path] = Depends(get_linear_project_path)
) -> dict:
    return get_linear_issue(issue_id, project_path=project_path)


@router.post("/issues", response_model=CreateLinearIssuesOut)
def post_create_issues(
    body: CreateLinearIssuesIn,
    project_path: Optional[Path] = Depends(get_linear_project_path),
) -> CreateLinearIssuesOut:
    """Create a batch of issues, isolating per-item failures the same way
    the ClickUp bulk-create route does."""
    # Parents are created before their children so a subticket can be nested
    # under a parent that already exists. Ordering only — the response still
    # lists one result per input ticket, in the caller's original order.
    order = sorted(range(len(body.tickets)), key=lambda i: bool(body.tickets[i].parent_title))
    created_ids: dict[str, str] = {}
    results: list[Optional[LinearIssueCreateResult]] = [None] * len(body.tickets)

    for index in order:
        ticket = body.tickets[index]
        try:
            issue_id = create_linear_issue(
                body.team_id,
                ticket.title,
                format_linear_description(ticket),
                priority=ticket.priority,
                assignee_id=ticket.assignee_linear_id,
                due_date=ticket.due_date,
                project_id=body.project_id,
                parent_id=created_ids.get(ticket.parent_title) if ticket.parent_title else None,
                project_path=project_path,
            )
        except (LinearIssueError, ValueError) as exc:
            results[index] = LinearIssueCreateResult(ticket=ticket, ok=False, error=str(exc))
        else:
            created_ids[ticket.title] = issue_id
            results[index] = LinearIssueCreateResult(ticket=ticket, ok=True, linear_issue_id=issue_id)

    return CreateLinearIssuesOut(results=[r for r in results if r is not None])


@router.post("/issues/{issue_id}/state")
def post_set_state(
    issue_id: str,
    body: SetLinearStateIn,
    project_path: Optional[Path] = Depends(get_linear_project_path),
) -> dict:
    try:
        return update_linear_issue_state(issue_id, body.state_id, project_path=project_path)
    except LinearIssueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
