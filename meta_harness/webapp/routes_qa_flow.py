"""Web UI endpoints for the QA ticket review flow: review a ClickUp
ticket (dry-run by default), list recorded reviews, and replay one.

Thin wrappers around meta_harness.qa_flow — no logic of its own, same as
every other webapp route module in this repo.
"""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException

from meta_harness.clickup_bridge import ClickUpReadError
from meta_harness.qa_flow import (
    REVIEW_AGENT_NAME,
    QAFlowError,
    QATicketReview,
    persist_review,
    replay_qa_review,
    review_and_record,
)
from meta_harness.qa_findings import QAFindingStore
from meta_harness.run_archive import RunArchive
from meta_harness.webapp.deps import get_qa_store
from meta_harness.webapp.schemas import (
    CommitReviewIn,
    FindingOut,
    ReplayReviewIn,
    ReviewRequestIn,
    ReviewResultOut,
    ReviewRunOut,
)

router = APIRouter()


@router.post("", response_model=ReviewResultOut)
def post_review(body: ReviewRequestIn, store: QAFindingStore = Depends(get_qa_store)) -> ReviewResultOut:
    try:
        review, finding, record = review_and_record(
            body.ticket_id, project=body.project, clickup_list_id=body.clickup_list_id,
            persist=body.persist, store=store,
        )
    except (ClickUpReadError, QAFlowError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return ReviewResultOut(run_id=record.run_id, review=review, finding=finding)


@router.get("", response_model=List[ReviewRunOut])
def get_reviews() -> List[ReviewRunOut]:
    records = RunArchive(REVIEW_AGENT_NAME).load()
    return [
        ReviewRunOut(run_id=record.run_id, ticket_id=record.subject_id, started_at=record.started_at, ok=record.ok)
        for record in records
    ]


@router.post("/commit", response_model=FindingOut)
def post_commit_review(body: CommitReviewIn, store: QAFindingStore = Depends(get_qa_store)):
    """Persist an already-computed review exactly as previewed — no
    re-fetch, no re-analysis, so what you saw is what gets saved."""
    review = QATicketReview(
        ticket_id=body.ticket_id, ticket_name=body.ticket_name,
        observation=body.observation, severity=body.severity,
    )
    try:
        return persist_review(review, project=body.project, clickup_list_id=body.clickup_list_id, store=store)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{run_id}/replay", response_model=ReviewResultOut)
def post_replay(
    run_id: str, body: ReplayReviewIn, store: QAFindingStore = Depends(get_qa_store)
) -> ReviewResultOut:
    try:
        _original, review, finding, record = replay_qa_review(
            run_id, persist=body.persist, clickup_list_id=body.clickup_list_id, store=store,
        )
    except (ClickUpReadError, QAFlowError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ReviewResultOut(run_id=record.run_id, review=review, finding=finding)
