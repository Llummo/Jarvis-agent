"""Pydantic request/response models for the web UI API."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict


class FindingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project: str
    route: str
    observation: str
    severity: str
    status: str
    screenshot_path: Optional[str] = None
    correction_note: Optional[str] = None
    clickup_task_id: Optional[str] = None
    created_at: str
    updated_at: str


class FindingsListOut(BaseModel):
    findings: list[FindingOut]
    counts: dict[str, int]


class ReportFindingIn(BaseModel):
    project: str
    route: str
    observation: str
    severity: str
    screenshot_path: Optional[str] = None
    clickup_list_id: Optional[str] = None
    auto_escalate: bool = True


class CloseFindingIn(BaseModel):
    correction_note: str


class ProposedTicketOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    title: str
    description: str
    acceptance_criteria: list[str]
    priority: str
    category: str


class GenerateTicketsOut(BaseModel):
    tickets: list[ProposedTicketOut]
    warnings: list[str]


class ProposedTicketIn(BaseModel):
    title: str
    description: str
    acceptance_criteria: list[str] = []
    priority: str = "normal"
    category: str = "mundane"


class CreateTicketsIn(BaseModel):
    tickets: list[ProposedTicketIn]
    list_id: Optional[str] = None


class TicketCreateResult(BaseModel):
    ticket: ProposedTicketIn
    ok: bool
    clickup_task_id: Optional[str] = None
    error: Optional[str] = None


class CreateTicketsOut(BaseModel):
    results: list[TicketCreateResult]


class QATicketReviewOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ticket_id: str
    ticket_name: str
    observation: str
    severity: str


class ReviewRequestIn(BaseModel):
    ticket_id: str
    project: str
    persist: bool = False
    clickup_list_id: Optional[str] = None
    progress_token: Optional[str] = None


class ReviewRunOut(BaseModel):
    run_id: str
    ticket_id: Optional[str] = None
    started_at: str
    ok: bool


class ReviewResultOut(BaseModel):
    run_id: str
    review: QATicketReviewOut
    finding: Optional[FindingOut] = None


class ReplayReviewIn(BaseModel):
    persist: bool = False
    clickup_list_id: Optional[str] = None
    progress_token: Optional[str] = None


class ProgressOut(BaseModel):
    steps: list[str]


class CommitReviewIn(BaseModel):
    ticket_id: str
    ticket_name: str
    observation: str
    severity: str
    project: str
    clickup_list_id: Optional[str] = None
