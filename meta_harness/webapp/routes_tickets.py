"""Requirements-doc -> proposed tickets -> bulk ClickUp creation.

Independent of the QA findings system — no route/severity concepts here,
just a generic "analyze a document, propose tickets, create some/all of
them" flow.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from meta_harness.clickup_bridge import ClickUpTicketError, create_clickup_ticket
from meta_harness.ticket_generator import (
    ClaudeNotFoundError,
    TicketExtractionError,
    TicketGenerationError,
    TicketParseError,
    generate_tickets_from_file,
)
from meta_harness.webapp.deps import get_clickup_project_path
from meta_harness.webapp.schemas import (
    CreateTicketsIn,
    CreateTicketsOut,
    GenerateTicketsOut,
    ProposedTicketIn,
    TicketCreateResult,
)

router = APIRouter()


@router.post("/generate", response_model=GenerateTicketsOut)
def post_generate_tickets(file: UploadFile = File(...)) -> GenerateTicketsOut:
    content = file.file.read()
    try:
        tickets, warnings = generate_tickets_from_file(file.filename or "document", content)
    except TicketExtractionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (ClaudeNotFoundError, TicketGenerationError, TicketParseError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return GenerateTicketsOut(tickets=tickets, warnings=warnings)


def _format_description(ticket: ProposedTicketIn) -> str:
    lines = [ticket.description, "", "Acceptance Criteria:"]
    lines += [f"- {criterion}" for criterion in ticket.acceptance_criteria] or ["- (none specified)"]
    return "\n".join(lines)


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
                project_path=project_path,
            )
        except (ClickUpTicketError, ValueError) as exc:
            results.append(TicketCreateResult(ticket=ticket, ok=False, error=str(exc)))
        else:
            results.append(TicketCreateResult(ticket=ticket, ok=True, clickup_task_id=task_id))
    return CreateTicketsOut(results=results)
