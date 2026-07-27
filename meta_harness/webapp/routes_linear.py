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
    first: int = 50,
    project_path: Optional[Path] = Depends(get_linear_project_path),
) -> list:
    return list_linear_issues(team_id, first=first, project_path=project_path)


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
    results: list[LinearIssueCreateResult] = []
    for ticket in body.tickets:
        try:
            issue_id = create_linear_issue(
                body.team_id,
                ticket.title,
                _format_description(ticket),
                priority=ticket.priority,
                assignee_id=ticket.assignee_linear_id,
                due_date=ticket.due_date,
                project_id=body.project_id,
                project_path=project_path,
            )
        except (LinearIssueError, ValueError) as exc:
            results.append(LinearIssueCreateResult(ticket=ticket, ok=False, error=str(exc)))
        else:
            results.append(LinearIssueCreateResult(ticket=ticket, ok=True, linear_issue_id=issue_id))
    return CreateLinearIssuesOut(results=results)


def _format_description(ticket) -> str:
    """Same rich template as the ClickUp route's _format_description —
    duplicated rather than shared, since the two trackers' route modules
    don't otherwise depend on each other and the template itself is just
    plain text composition, not business logic worth abstracting."""
    epic = ticket.epic or ticket.title.upper()
    lines = [
        f"📄 USER STORY: {epic}",
        f"Título: {ticket.title}",
        "",
        f"📍 Ruta / Vista UI: {ticket.ui_route or '(no aplica)'}",
        f"🔌 Endpoint Backend: {ticket.backend_endpoint or '(no aplica)'}",
        "",
        "📝 DESCRIPCIÓN",
    ]
    if ticket.user_story:
        lines.append(ticket.user_story)
        lines.append("")
    lines.append(ticket.description)
    lines += [
        "",
        "🖼️ RECURSOS VISUALES Y REFERENCIAS (OPCIONAL)",
        "- No se proporcionaron recursos visuales; agregar capturas, diagramas o enlaces de referencia si están disponibles.",
        "",
        "✅ CRITERIOS DE ACEPTACIÓN",
    ]
    for index, criterion in enumerate(ticket.acceptance_criteria, start=1):
        lines.append(f"📌 Criterio {index}: {criterion}")
    lines.append("📌 Criterio X: [Espacio para criterios adicionales]")
    lines += [
        "",
        "🛠️ NOTAS TÉCNICAS Y ADICIONALES (opcional)",
        ticket.technical_notes or "(sin notas adicionales)",
    ]
    return "\n".join(lines)


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
