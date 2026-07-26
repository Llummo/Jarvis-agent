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
