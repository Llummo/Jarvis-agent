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

from meta_harness.clickup_bridge import ClickUpReadError, ClickUpTicketError, create_clickup_ticket
from meta_harness.linear_bridge import LinearIssueError, LinearReadError, list_linear_members
from meta_harness.module_relevance import ClaudeNotFoundError as ModuleClaudeNotFoundError
from meta_harness.module_relevance import (
    ModuleRelevanceError,
    analyze_module_relevance,
    analyze_modules_bulk,
    render_module_report_markdown,
    sort_by_relevance,
)
from meta_harness.team_assignment import (
    assign_random_members,
    list_team_members,
    merge_member_rosters,
    parse_emails,
    verify_team_emails,
)
from meta_harness.ticket_format import format_clickup_description
from meta_harness.reformat_history import ReformatHistoryStore
from meta_harness.ticket_reformat import (
    NoPreviousVersionError,
    apply_reformatted_ticket,
    reformat_ticket,
    revert_ticket,
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
    ModuleRelevanceBulkIn,
    ModuleRelevanceBulkOut,
    ModuleRelevanceIn,
    ModuleRelevanceItemOut,
    ModuleRelevanceOut,
    ModuleRelevanceSummaryOut,
    ApplyReformatIn,
    ApplyReformatOut,
    ReformatTicketIn,
    ReformatTicketOut,
    RevertibleTicketOut,
    RevertTicketIn,
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
    team_assignment.merge_member_rosters).

    Either tracker being unreachable degrades to the other one's roster
    rather than failing outright, so a Linear-only (or ClickUp-only) setup
    still gets a usable member list."""
    try:
        clickup_members = list_team_members()
    except ClickUpReadError as exc:
        if on_step:
            on_step(f"Could not load the ClickUp roster: {exc}")
        clickup_members = []
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


@router.get("/team-members", response_model=list[TeamMemberOut])
def get_team_members(
    tracker: str = "clickup", linear_team_id: Optional[str] = None
) -> list[TeamMemberOut]:
    """The real people the harness can actually assign work to in `tracker`.

    Only members carrying an id for that tracker are returned, so every
    name the UI offers is genuinely assignable there — no typing a list of
    emails by hand and hoping they match.
    """
    roster = _merged_roster(linear_team_id)
    id_field = "linear_id" if tracker == "linear" else "clickup_id"
    return [
        TeamMemberOut(
            email=member["email"],
            username=member["username"],
            clickup_id=member.get("clickup_id"),
            linear_id=member.get("linear_id"),
        )
        for member in roster
        if member.get(id_field)
    ]


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
    # Parents first, so a subtask can be nested under a task that already
    # exists — see the matching comment in routes_linear.post_create_issues.
    order = sorted(range(len(body.tickets)), key=lambda i: bool(body.tickets[i].parent_title))
    created_ids: dict[str, str] = {}
    results: list[Optional[TicketCreateResult]] = [None] * len(body.tickets)

    for index in order:
        ticket = body.tickets[index]
        try:
            task_id = create_clickup_ticket(
                ticket.title,
                format_clickup_description(ticket),
                list_id=body.list_id,
                priority=ticket.priority,
                assignees=[ticket.assignee_clickup_id] if ticket.assignee_clickup_id else None,
                due_date_ms=_due_date_ms(ticket.due_date),
                parent=created_ids.get(ticket.parent_title) if ticket.parent_title else None,
                project_path=project_path,
            )
        except (ClickUpTicketError, ValueError) as exc:
            results[index] = TicketCreateResult(ticket=ticket, ok=False, error=str(exc))
        else:
            created_ids[ticket.title] = task_id
            results[index] = TicketCreateResult(ticket=ticket, ok=True, clickup_task_id=task_id)

    return CreateTicketsOut(results=[r for r in results if r is not None])


@router.post("/module-relevance", response_model=ModuleRelevanceOut)
def post_module_relevance(body: ModuleRelevanceIn) -> ModuleRelevanceOut:
    """Judge whether a real ticket belongs to a product module, given that
    module's official documentation as context. Read-only on both trackers."""
    on_step = None
    if body.progress_token:
        progress.start(body.progress_token)
        on_step = lambda message: progress.push(body.progress_token, message)  # noqa: E731
    try:
        return analyze_module_relevance(
            body.ticket_id,
            tracker=body.tracker,
            module_name=body.module_name,
            module_context=body.module_context,
            on_step=on_step,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ModuleClaudeNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (ClickUpReadError, LinearReadError, ModuleRelevanceError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/module-relevance/bulk", response_model=ModuleRelevanceBulkOut)
def post_module_relevance_bulk(body: ModuleRelevanceBulkIn) -> ModuleRelevanceBulkOut:
    """Check every given ticket against one module in a single sweep and
    report which of them align with it. Read-only on both trackers."""
    on_step = None
    if body.progress_token:
        progress.start(body.progress_token)
        on_step = lambda message: progress.push(body.progress_token, message)  # noqa: E731
    try:
        results = analyze_modules_bulk(
            body.ticket_ids,
            tracker=body.tracker,
            module_name=body.module_name,
            module_context=body.module_context,
            on_step=on_step,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ModuleClaudeNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    ordered = sort_by_relevance(results)
    analyzed = [rel for _tid, rel, err in ordered if rel and not err]
    aligned = [rel for rel in analyzed if rel.is_related]
    summary = ModuleRelevanceSummaryOut(
        analyzed=len(analyzed),
        related=sum(1 for rel in analyzed if rel.verdict == "related"),
        partially_related=sum(1 for rel in analyzed if rel.verdict == "partially_related"),
        unrelated=sum(1 for rel in analyzed if rel.verdict == "unrelated"),
        failed=len(results) - len(analyzed),
    )
    return ModuleRelevanceBulkOut(
        module_name=body.module_name,
        summary=summary,
        aligned=aligned,
        results=[
            ModuleRelevanceItemOut(ticket_id=tid, relevance=rel, error=err) for tid, rel, err in ordered
        ],
        report_markdown=render_module_report_markdown(body.module_name, results),
    )


@router.post("/reformat", response_model=ReformatTicketOut)
def post_reformat_ticket(body: ReformatTicketIn) -> ReformatTicketOut:
    """Propose a reformatted version of an existing ticket. Read-only —
    nothing is written until /reformat/apply is called."""
    on_step = None
    if body.progress_token:
        progress.start(body.progress_token)
        on_step = lambda message: progress.push(body.progress_token, message)  # noqa: E731
    try:
        return reformat_ticket(body.ticket_id, tracker=body.tracker, on_step=on_step)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ClaudeNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (ClickUpReadError, LinearReadError, TicketGenerationError, TicketParseError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/reformat/apply", response_model=ApplyReformatOut)
def post_apply_reformat(body: ApplyReformatIn) -> ApplyReformatOut:
    """Write an already-reviewed rewrite back to the tracker."""
    on_step = None
    if body.progress_token:
        progress.start(body.progress_token)
        on_step = lambda message: progress.push(body.progress_token, message)  # noqa: E731
    try:
        updated = apply_reformatted_ticket(
            body.ticket_id,
            tracker=body.tracker,
            title=body.title,
            description=body.description,
            on_step=on_step,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (ClickUpTicketError, LinearIssueError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return ApplyReformatOut(
        ok=True, ticket_id=body.ticket_id, title=updated.get("title") or updated.get("name")
    )


@router.get("/reformat/revertible", response_model=list[RevertibleTicketOut])
def get_revertible(tracker: Optional[str] = None) -> list[RevertibleTicketOut]:
    """Tickets whose pre-reformat version is still saved, newest first."""
    return [RevertibleTicketOut(**entry) for entry in ReformatHistoryStore().list_for(tracker)]


@router.post("/reformat/revert", response_model=ApplyReformatOut)
def post_revert_reformat(body: RevertTicketIn) -> ApplyReformatOut:
    """Restore a ticket to the exact version saved before it was reformatted."""
    on_step = None
    if body.progress_token:
        progress.start(body.progress_token)
        on_step = lambda message: progress.push(body.progress_token, message)  # noqa: E731
    try:
        updated = revert_ticket(body.ticket_id, tracker=body.tracker, on_step=on_step)
    except NoPreviousVersionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (ClickUpTicketError, LinearIssueError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return ApplyReformatOut(
        ok=True, ticket_id=body.ticket_id, title=updated.get("title") or updated.get("name")
    )
