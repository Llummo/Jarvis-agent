"""QA findings dashboard endpoints: list+filter+counts, report, close."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from meta_harness.qa_findings import (
    SEVERITIES,
    STATUSES,
    QAFindingNotFoundError,
    QAFindingStore,
    close_qa_issue,
    list_qa_issues,
    report_qa_issue,
)
from meta_harness.webapp.deps import get_qa_store
from meta_harness.webapp.schemas import CloseFindingIn, FindingOut, FindingsListOut, ReportFindingIn

router = APIRouter()


def _counts(findings) -> dict:
    counts = {"total": len(findings)}
    for key in STATUSES:
        counts[key] = sum(1 for f in findings if f.status == key)
    for key in SEVERITIES:
        counts[key] = sum(1 for f in findings if f.severity == key)
    return counts


@router.get("/findings", response_model=FindingsListOut)
def get_findings(
    project: Optional[str] = None,
    severity: Optional[str] = None,
    status: Optional[str] = None,
    store: QAFindingStore = Depends(get_qa_store),
) -> FindingsListOut:
    findings = list_qa_issues(project=project, severity=severity, status=status, store=store)
    all_findings = findings if not (project or severity or status) else list_qa_issues(store=store)
    return FindingsListOut(findings=findings, counts=_counts(all_findings))


@router.post("/findings", response_model=FindingOut)
def post_finding(body: ReportFindingIn, store: QAFindingStore = Depends(get_qa_store)) -> FindingOut:
    try:
        finding = report_qa_issue(
            body.project,
            body.route,
            body.observation,
            body.severity,
            screenshot_path=body.screenshot_path,
            clickup_list_id=body.clickup_list_id,
            auto_escalate=body.auto_escalate,
            store=store,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return finding


@router.post("/findings/{finding_id}/close", response_model=FindingOut)
def post_close_finding(
    finding_id: int, body: CloseFindingIn, store: QAFindingStore = Depends(get_qa_store)
) -> FindingOut:
    try:
        finding = close_qa_issue(finding_id, body.correction_note, store=store)
    except QAFindingNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return finding
