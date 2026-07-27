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
    checked_route: Optional[str] = None
    status_code: Optional[int] = None
    http_error: Optional[str] = None
    correction_note: Optional[str] = None
    clickup_task_id: Optional[str] = None
    linear_issue_id: Optional[str] = None
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
    tracker: str = "clickup"
    clickup_list_id: Optional[str] = None
    linear_team_id: Optional[str] = None
    auto_escalate: bool = True


class CloseFindingIn(BaseModel):
    correction_note: str


class AcceptanceCriterionModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str = ""
    given: str = ""
    when: str = ""
    then: str = ""
    text: str = ""


class ProposedTicketOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    title: str
    description: str
    user_story: str = ""
    epic: str = ""
    ui_route: str = ""
    backend_endpoint: str = ""
    technical_notes: str = ""
    parent_title: str = ""
    acceptance_criteria: list[AcceptanceCriterionModel]
    priority: str
    category: str
    sprint: int = 1
    due_date: Optional[str] = None
    assignee_clickup_id: Optional[int] = None
    assignee_linear_id: Optional[str] = None
    assignee_email: Optional[str] = None
    assignee_name: Optional[str] = None


class GenerateTicketsOut(BaseModel):
    tickets: list[ProposedTicketOut]
    warnings: list[str]


class ProposedTicketIn(BaseModel):
    title: str
    description: str
    user_story: str = ""
    epic: str = ""
    ui_route: str = ""
    backend_endpoint: str = ""
    technical_notes: str = ""
    parent_title: str = ""
    acceptance_criteria: list[AcceptanceCriterionModel] = []
    priority: str = "normal"
    category: str = "mundane"
    sprint: int = 1
    due_date: Optional[str] = None
    assignee_clickup_id: Optional[int] = None
    assignee_linear_id: Optional[str] = None
    assignee_email: Optional[str] = None
    assignee_name: Optional[str] = None


class VerifyTeamIn(BaseModel):
    emails_text: str
    linear_team_id: Optional[str] = None


class TeamMemberOut(BaseModel):
    email: str
    username: str
    clickup_id: Optional[int] = None
    linear_id: Optional[str] = None


class VerifyTeamOut(BaseModel):
    verified: list[TeamMemberOut]
    not_found: list[str]


class ChatTurnIn(BaseModel):
    role: str  # "user" | "assistant"
    content: str


class GenerateFromIdeaIn(BaseModel):
    idea: str
    history: list[ChatTurnIn] = []
    start_mundane: int = 1
    start_backend: int = 1
    start_frontend: int = 1
    start_deployment: int = 1
    progress_token: Optional[str] = None


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
    route: Optional[str] = None
    status_code: Optional[int] = None
    http_error: Optional[str] = None
    screenshot_path: Optional[str] = None


class ReviewRequestIn(BaseModel):
    ticket_id: str
    project: str
    persist: bool = False
    tracker: str = "clickup"
    clickup_list_id: Optional[str] = None
    linear_team_id: Optional[str] = None
    progress_token: Optional[str] = None
    pass_status: Optional[str] = None
    fail_status: Optional[str] = None


class ReviewRunOut(BaseModel):
    run_id: str
    ticket_id: Optional[str] = None
    started_at: str
    ok: bool


class ReviewResultOut(BaseModel):
    run_id: str
    review: QATicketReviewOut
    finding: Optional[FindingOut] = None
    report_markdown: str = ""


class ReplayReviewIn(BaseModel):
    persist: bool = False
    clickup_list_id: Optional[str] = None
    linear_team_id: Optional[str] = None
    progress_token: Optional[str] = None
    pass_status: Optional[str] = None
    fail_status: Optional[str] = None


class ProgressOut(BaseModel):
    steps: list[str]


class CommitReviewIn(BaseModel):
    ticket_id: str
    ticket_name: str
    observation: str
    severity: str
    project: str
    tracker: str = "clickup"
    clickup_list_id: Optional[str] = None
    linear_team_id: Optional[str] = None
    pass_status: Optional[str] = None
    fail_status: Optional[str] = None
    progress_token: Optional[str] = None
    route: Optional[str] = None
    status_code: Optional[int] = None
    http_error: Optional[str] = None
    screenshot_path: Optional[str] = None


class BulkReviewIn(BaseModel):
    ticket_ids: list[str]
    project: str
    tracker: str = "clickup"
    clickup_list_id: Optional[str] = None
    linear_team_id: Optional[str] = None
    progress_token: Optional[str] = None
    pass_status: Optional[str] = None
    fail_status: Optional[str] = None


class BulkReviewItemOut(BaseModel):
    ticket_id: str
    review: Optional[QATicketReviewOut] = None
    error: Optional[str] = None


class BulkReviewOut(BaseModel):
    results: list[BulkReviewItemOut]
    readme_markdown: str = ""


class BulkCommitItemIn(BaseModel):
    ticket_id: str
    ticket_name: str
    observation: str
    severity: str
    project: str
    tracker: str = "clickup"
    clickup_list_id: Optional[str] = None
    linear_team_id: Optional[str] = None
    pass_status: Optional[str] = None
    fail_status: Optional[str] = None
    route: Optional[str] = None
    status_code: Optional[int] = None
    http_error: Optional[str] = None
    screenshot_path: Optional[str] = None


class BulkCommitIn(BaseModel):
    items: list[BulkCommitItemIn]
    progress_token: Optional[str] = None


class BulkCommitResultOut(BaseModel):
    ticket_id: str
    finding: Optional[FindingOut] = None
    error: Optional[str] = None


class BulkCommitOut(BaseModel):
    results: list[BulkCommitResultOut]


class ProjectConfigIn(BaseModel):
    project: str
    base_url: str


class ProjectConfigOut(BaseModel):
    projects: dict[str, str]


class CreateLinearIssuesIn(BaseModel):
    team_id: str
    tickets: list[ProposedTicketIn]
    project_id: Optional[str] = None


class LinearIssueCreateResult(BaseModel):
    ticket: ProposedTicketIn
    ok: bool
    linear_issue_id: Optional[str] = None
    error: Optional[str] = None


class CreateLinearIssuesOut(BaseModel):
    results: list[LinearIssueCreateResult]


class SetLinearStateIn(BaseModel):
    state_id: str


class ModuleRelevanceIn(BaseModel):
    ticket_id: str
    module_name: str
    module_context: str
    tracker: str = "clickup"
    progress_token: Optional[str] = None


class ModuleRelevanceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ticket_id: str
    ticket_name: str
    module_name: str
    verdict: str
    confidence: float
    rationale: str
    matched_aspects: list[str]
    module_gaps: list[str]
