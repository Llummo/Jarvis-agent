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
from meta_harness.team_assignment import assign_random_members, parse_emails, verify_team_emails
from meta_harness.ticket_generator import (
    ClaudeNotFoundError,
    TicketExtractionError,
    TicketGenerationError,
    TicketParseError,
    apply_category_numbering,
    apply_sprint_due_dates,
    generate_tickets_from_file,
)
from meta_harness.webapp import progress
from meta_harness.webapp.deps import get_clickup_project_path
from meta_harness.webapp.schemas import (
    CreateTicketsIn,
    CreateTicketsOut,
    GenerateTicketsOut,
    ProposedTicketIn,
    TeamMemberOut,
    TicketCreateResult,
    VerifyTeamIn,
    VerifyTeamOut,
)

router = APIRouter()


@router.post("/generate", response_model=GenerateTicketsOut)
def post_generate_tickets(
    file: UploadFile = File(...),
    start_mundane: int = Form(1),
    start_backend: int = Form(1),
    start_frontend: int = Form(1),
    start_deployment: int = Form(1),
    team_emails_text: Optional[str] = Form(None),
    progress_token: Optional[str] = Form(None),
) -> GenerateTicketsOut:
    content = file.file.read()
    on_step = None
    if progress_token:
        progress.start(progress_token)
        on_step = lambda message: progress.push(progress_token, message)  # noqa: E731
    try:
        tickets, warnings = generate_tickets_from_file(file.filename or "document", content, on_step=on_step)
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
    tickets = apply_sprint_due_dates(tickets)

    if team_emails_text and team_emails_text.strip():
        if on_step:
            on_step("Verifying team members…")
        emails = parse_emails(team_emails_text)
        verified, not_found = verify_team_emails(emails)
        if verified:
            if on_step:
                on_step(f"Randomly assigning {len(verified)} verified member(s) to tickets…")
            tickets = assign_random_members(tickets, verified)
        elif emails:
            warnings.append("None of the given team emails matched a real ClickUp workspace member — tickets left unassigned.")
        if not_found:
            warnings.append(f"Team emails not found in ClickUp: {', '.join(not_found)}")

    if on_step:
        on_step("Done.")

    return GenerateTicketsOut(tickets=tickets, warnings=warnings)


@router.post("/verify-team", response_model=VerifyTeamOut)
def post_verify_team(body: VerifyTeamIn) -> VerifyTeamOut:
    emails = parse_emails(body.emails_text)
    verified, not_found = verify_team_emails(emails)
    return VerifyTeamOut(
        verified=[TeamMemberOut(user_id=m["id"], email=m["email"], username=m["username"]) for m in verified],
        not_found=not_found,
    )


def _format_description(ticket: ProposedTicketIn) -> str:
    lines = []
    if ticket.user_story:
        lines += [ticket.user_story, ""]
    lines += [ticket.description, "", "Acceptance Criteria:"]
    lines += [f"- {criterion}" for criterion in ticket.acceptance_criteria] or ["- (none specified)"]
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
                assignees=[ticket.assignee_user_id] if ticket.assignee_user_id else None,
                due_date_ms=_due_date_ms(ticket.due_date),
                project_path=project_path,
            )
        except (ClickUpTicketError, ValueError) as exc:
            results.append(TicketCreateResult(ticket=ticket, ok=False, error=str(exc)))
        else:
            results.append(TicketCreateResult(ticket=ticket, ok=True, clickup_task_id=task_id))
    return CreateTicketsOut(results=results)
