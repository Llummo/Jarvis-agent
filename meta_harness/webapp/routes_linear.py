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
                _format_description(ticket),
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


def _format_description(ticket) -> str:
    """Linear's house ticket format, followed strictly.

    Deliberately NOT shared with the ClickUp renderer: the two trackers
    present tickets differently on purpose, and this one has to match the
    team's template exactly — section order, the Como/quiero/para split
    across three lines, named criteria with Given/When/Then on their own
    bullets, the trailing "Criterio X" template, and bulleted technical
    notes.
    """
    epic = ticket.epic or ticket.title.upper()
    lines = [
        f"📄 USER STORY: {epic}",
        "",
        f"Título: {ticket.title}",
        "",
        f"📍 Ruta / Vista UI: {ticket.ui_route or '(no aplica)'}",
        "",
        f"🔌 Endpoint Backend: {ticket.backend_endpoint or '(no aplica)'}",
        "",
        "📝 DESCRIPCIÓN",
    ]

    # "Como …, / quiero …, / para …" each on its own line, per the template.
    if ticket.user_story:
        story_lines = [line.strip() for line in ticket.user_story.splitlines() if line.strip()]
        for story_line in story_lines:
            lines += [story_line, ""]
    lines += [ticket.description, ""]

    lines += [
        "🖼️ RECURSOS VISUALES Y REFERENCIAS (OPCIONAL)",
        "* Mockup / UI Route: [Link a Figma]",
        "* Diagrama / Adjuntos: [Adjuntar diagramas o capturas de referencia]",
        "",
        "- En general sería ideal agregar cualquier imagen de referencia.",
        "",
        "✅ CRITERIOS DE ACEPTACIÓN",
        "",
    ]
    for index, criterion in enumerate(ticket.acceptance_criteria, start=1):
        name = criterion.name or f"Criterio {index}"
        lines.append(f"📌 Criterio {index}: {name}")
        if criterion.given or criterion.when or criterion.then:
            lines += [
                f"* Dado que {criterion.given},",
                f"* cuando {criterion.when},",
                f"* entonces {criterion.then}.",
            ]
        elif criterion.text:
            lines.append(f"* {criterion.text}")
        lines.append("")

    lines += [
        "📌 Criterio X: [Espacio para criterios adicionales]",
        "* Dado que [condición inicial],",
        "* cuando [acción o evento],",
        "* entonces [resultado esperado].",
        "",
        "🛠️ NOTAS TÉCNICAS Y ADICIONALES (opcional)",
    ]
    if ticket.technical_notes:
        for note in [n.strip() for n in ticket.technical_notes.splitlines() if n.strip()]:
            lines.append(note if note.startswith("*") else f"* {note}")
    else:
        lines.append("* (sin notas adicionales)")

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
