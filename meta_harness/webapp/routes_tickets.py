"""Requirements-doc -> proposed tickets -> bulk ClickUp creation.

Independent of the QA findings system — no route/severity concepts here,
just a generic "analyze a document, propose tickets, create some/all of
them" flow.
"""

from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from meta_harness.clickup_bridge import ClickUpTicketError, create_clickup_ticket
from meta_harness.linear_bridge import LinearReadError, list_linear_members
from meta_harness.team_assignment import (
    assign_random_members,
    list_team_members,
    merge_member_rosters,
    parse_emails,
    verify_team_emails,
)
from meta_harness.ticket_generator import (
    ClaudeNotFoundError,
    TicketExtractionError,
    TicketGenerationError,
    TicketParseError,
    apply_category_numbering,
    apply_sprint_due_dates,
    generate_tickets_from_file,
    generate_tickets_from_idea,
)
from meta_harness.webapp import progress
from meta_harness.webapp.deps import get_clickup_project_path
from meta_harness.webapp.schemas import (
    CreateTicketsIn,
    CreateTicketsOut,
    GenerateFromIdeaIn,
    GenerateTicketsOut,
    ProposedTicketIn,
    TeamMemberOut,
    TicketCreateResult,
    VerifyTeamIn,
    VerifyTeamOut,
)

router = APIRouter()


def _merged_roster(linear_team_id: Optional[str], *, on_step=None) -> list[dict]:
    """The ClickUp roster, merged with a Linear team's roster when a team
    id is given — a member found in either tracker counts as verifiable,
    carrying whichever tracker-specific id(s) matched (see
    team_assignment.merge_member_rosters)."""
    clickup_members = list_team_members()
    if not linear_team_id:
        return clickup_members
    try:
        linear_members = list_linear_members(linear_team_id)
    except LinearReadError as exc:
        if on_step:
            on_step(f"Could not load Linear roster, verifying against ClickUp only: {exc}")
        return clickup_members
    return merge_member_rosters(clickup_members, linear_members)


@router.post("/generate", response_model=GenerateTicketsOut)
def post_generate_tickets(
    file: UploadFile = File(...),
    start_mundane: int = Form(1),
    start_backend: int = Form(1),
    start_frontend: int = Form(1),
    start_deployment: int = Form(1),
    team_emails_text: Optional[str] = Form(None),
    linear_team_id: Optional[str] = Form(None),
    project_start: Optional[str] = Form(None),
    project_end: Optional[str] = Form(None),
    progress_token: Optional[str] = Form(None),
) -> GenerateTicketsOut:
    try:
        parsed_start = date.fromisoformat(project_start) if project_start else None
        parsed_end = date.fromisoformat(project_end) if project_end else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid date: {exc}") from exc
    if parsed_start and parsed_end and parsed_end <= parsed_start:
        raise HTTPException(status_code=400, detail="Project end date must be after the start date.")

    content = file.file.read()
    on_step = None
    if progress_token:
        progress.start(progress_token)
        on_step = lambda message: progress.push(progress_token, message)  # noqa: E731
    try:
        tickets, warnings = generate_tickets_from_file(
            file.filename or "document", content, on_step=on_step,
            project_start=parsed_start, project_end=parsed_end,
        )
    except TicketExtractionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ClaudeNotFoundError as exc:
        # A configuration/availability problem (no claude on PATH), distinct
        # from the agent running but producing a bad result.
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (TicketGenerationError, TicketParseError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if on_step:
        on_step(f"Numbering {len(tickets)} ticket(s)…")
    start_numbers = {
        "mundane": start_mundane,
        "backend": start_backend,
        "frontend": start_frontend,
        "deployment": start_deployment,
    }
    tickets = apply_category_numbering(tickets, start_numbers)

    if on_step:
        on_step("Assigning sprint due dates…")
    tickets = apply_sprint_due_dates(tickets, sprint_start=parsed_start, project_end=parsed_end)

    if team_emails_text and team_emails_text.strip():
        if on_step:
            on_step("Verifying team members…")
        emails = parse_emails(team_emails_text)
        roster = _merged_roster(linear_team_id, on_step=on_step)
        verified, not_found = verify_team_emails(emails, members=roster)
        if verified:
            if on_step:
                on_step(f"Randomly assigning {len(verified)} verified member(s) to tickets…")
            tickets = assign_random_members(tickets, verified)
        elif emails:
            warnings.append("None of the given team emails matched a real ClickUp or Linear workspace member — tickets left unassigned.")
        if not_found:
            warnings.append(f"Team emails not found in ClickUp/Linear: {', '.join(not_found)}")

    if on_step:
        on_step("Done.")

    return GenerateTicketsOut(tickets=tickets, warnings=warnings)


@router.post("/from-idea", response_model=GenerateTicketsOut)
def post_generate_from_idea(body: GenerateFromIdeaIn) -> GenerateTicketsOut:
    """Turn one free-text idea (the chat mode) into properly-formatted
    proposed tickets — same output shape and same numbering as the
    document path, so both feed the identical review-and-add UI."""
    on_step = None
    if body.progress_token:
        progress.start(body.progress_token)
        on_step = lambda message: progress.push(body.progress_token, message)  # noqa: E731
    try:
        tickets, warnings = generate_tickets_from_idea(
            body.idea,
            history=[turn.model_dump() for turn in body.history],
            on_step=on_step,
        )
    except ClaudeNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (TicketGenerationError, TicketParseError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    tickets = apply_category_numbering(
        tickets,
        {
            "mundane": body.start_mundane,
            "backend": body.start_backend,
            "frontend": body.start_frontend,
            "deployment": body.start_deployment,
        },
    )
    if on_step:
        on_step("Done.")
    return GenerateTicketsOut(tickets=tickets, warnings=warnings)


@router.post("/verify-team", response_model=VerifyTeamOut)
def post_verify_team(body: VerifyTeamIn) -> VerifyTeamOut:
    emails = parse_emails(body.emails_text)
    roster = _merged_roster(body.linear_team_id)
    verified, not_found = verify_team_emails(emails, members=roster)
    return VerifyTeamOut(
        verified=[
            TeamMemberOut(
                clickup_id=m.get("clickup_id"), linear_id=m.get("linear_id"),
                email=m["email"], username=m["username"],
            )
            for m in verified
        ],
        not_found=not_found,
    )


def _format_description(ticket: ProposedTicketIn) -> str:
    """Render the team's standard ticket description template:
    epic/title header, UI route + backend endpoint, a Spanish user story +
    concrete description, optional visual-references placeholder, numbered
    Gherkin acceptance criteria (plus a trailing placeholder for more), and
    optional technical notes."""
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


def _due_date_ms(due_date_iso: Optional[str]) -> Optional[int]:
    """Convert an ISO date string (e.g. from apply_sprint_due_dates) into
    the Unix-milliseconds timestamp ClickUp's API expects."""
    if not due_date_iso:
        return None
    parsed = date.fromisoformat(due_date_iso)
    return int(datetime.combine(parsed, time()).timestamp() * 1000)


@router.post("/create", response_model=CreateTicketsOut)
def post_create_tickets(
    body: CreateTicketsIn,
    project_path: Optional[Path] = Depends(get_clickup_project_path),
) -> CreateTicketsOut:
    results: list[TicketCreateResult] = []
    for ticket in body.tickets:
        try:
            task_id = create_clickup_ticket(
                ticket.title,
                _format_description(ticket),
                list_id=body.list_id,
                priority=ticket.priority,
                assignees=[ticket.assignee_clickup_id] if ticket.assignee_clickup_id else None,
                due_date_ms=_due_date_ms(ticket.due_date),
                project_path=project_path,
            )
        except (ClickUpTicketError, ValueError) as exc:
            results.append(TicketCreateResult(ticket=ticket, ok=False, error=str(exc)))
        else:
            results.append(TicketCreateResult(ticket=ticket, ok=True, clickup_task_id=task_id))
    return CreateTicketsOut(results=results)
