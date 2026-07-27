"""Web UI endpoints for the QA ticket review flow: review a ClickUp
ticket (dry-run by default), list recorded reviews, and replay one.

Thin wrappers around meta_harness.qa_flow — no logic of its own, same as
every other webapp route module in this repo.
"""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException

from meta_harness.clickup_bridge import ClickUpReadError
from meta_harness.project_config import ProjectConfigStore
from meta_harness.qa_flow import (
    REVIEW_AGENT_NAME,
    QAFlowError,
    QATicketReview,
    persist_review,
    replay_qa_review,
    review_and_record,
    review_tickets_bulk,
)
from meta_harness.qa_findings import QAFindingStore
from meta_harness.qa_report import render_bulk_readme_markdown, render_finding_markdown, render_review_markdown
from meta_harness.run_archive import RunArchive
from meta_harness.webapp import progress
from meta_harness.webapp.deps import get_project_config_store, get_qa_store
from meta_harness.webapp.schemas import (
    BulkCommitIn,
    BulkCommitOut,
    BulkCommitResultOut,
    BulkReviewIn,
    BulkReviewItemOut,
    BulkReviewOut,
    CommitReviewIn,
    FindingOut,
    ReplayReviewIn,
    ReviewRequestIn,
    ReviewResultOut,
    ReviewRunOut,
)

router = APIRouter()


@router.post("", response_model=ReviewResultOut)
def post_review(
    body: ReviewRequestIn,
    store: QAFindingStore = Depends(get_qa_store),
    project_config: ProjectConfigStore = Depends(get_project_config_store),
) -> ReviewResultOut:
    on_step = None
    if body.progress_token:
        progress.start(body.progress_token)
        on_step = lambda message: progress.push(body.progress_token, message)  # noqa: E731
    try:
        review, finding, record = review_and_record(
            body.ticket_id, project=body.project, clickup_list_id=body.clickup_list_id,
            persist=body.persist, store=store, on_step=on_step,
            pass_status=body.pass_status, fail_status=body.fail_status, project_config=project_config,
        )
    except (ClickUpReadError, QAFlowError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    report_markdown = render_finding_markdown(finding) if finding else render_review_markdown(review)
    return ReviewResultOut(run_id=record.run_id, review=review, finding=finding, report_markdown=report_markdown)


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
    on_step = None
    if body.progress_token:
        progress.start(body.progress_token)
        on_step = lambda message: progress.push(body.progress_token, message)  # noqa: E731
    review = QATicketReview(
        ticket_id=body.ticket_id, ticket_name=body.ticket_name,
        observation=body.observation, severity=body.severity,
        route=body.route, status_code=body.status_code,
        http_error=body.http_error, screenshot_path=body.screenshot_path,
    )
    try:
        return persist_review(
            review, project=body.project, clickup_list_id=body.clickup_list_id, store=store,
            pass_status=body.pass_status, fail_status=body.fail_status, on_step=on_step,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{run_id}/replay", response_model=ReviewResultOut)
def post_replay(
    run_id: str,
    body: ReplayReviewIn,
    store: QAFindingStore = Depends(get_qa_store),
    project_config: ProjectConfigStore = Depends(get_project_config_store),
) -> ReviewResultOut:
    on_step = None
    if body.progress_token:
        progress.start(body.progress_token)
        on_step = lambda message: progress.push(body.progress_token, message)  # noqa: E731
    try:
        _original, review, finding, record = replay_qa_review(
            run_id, persist=body.persist, clickup_list_id=body.clickup_list_id, store=store, on_step=on_step,
            pass_status=body.pass_status, fail_status=body.fail_status, project_config=project_config,
        )
    except (ClickUpReadError, QAFlowError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    report_markdown = render_finding_markdown(finding) if finding else render_review_markdown(review)
    return ReviewResultOut(run_id=record.run_id, review=review, finding=finding, report_markdown=report_markdown)


@router.post("/bulk", response_model=BulkReviewOut)
def post_bulk_review(
    body: BulkReviewIn, project_config: ProjectConfigStore = Depends(get_project_config_store)
) -> BulkReviewOut:
    """Dry-run QA across every given ticket at once — never persists;
    review /bulk/commit to save the ones you want. Also renders the whole
    sweep as one QA-README.md (performed checks, routes, status codes,
    screenshot evidence, proposed status move per ticket)."""
    on_step = None
    if body.progress_token:
        progress.start(body.progress_token)
        on_step = lambda message: progress.push(body.progress_token, message)  # noqa: E731
    results = review_tickets_bulk(
        body.ticket_ids, project=body.project, clickup_list_id=body.clickup_list_id, on_step=on_step,
        project_config=project_config,
    )
    readme_markdown = render_bulk_readme_markdown(
        body.project, results, pass_status=body.pass_status, fail_status=body.fail_status
    )
    return BulkReviewOut(
        results=[BulkReviewItemOut(ticket_id=tid, review=review, error=error) for tid, review, error in results],
        readme_markdown=readme_markdown,
    )


@router.post("/bulk/commit", response_model=BulkCommitOut)
def post_bulk_commit(body: BulkCommitIn, store: QAFindingStore = Depends(get_qa_store)) -> BulkCommitOut:
    """Persist a batch of already-computed reviews, one finding (and
    status move) per item — one item failing doesn't stop the rest,
    same per-item isolation as bulk ticket creation."""
    on_step = None
    if body.progress_token:
        progress.start(body.progress_token)
        on_step = lambda message: progress.push(body.progress_token, message)  # noqa: E731
    results = []
    total = len(body.items)
    for index, item in enumerate(body.items, start=1):
        if on_step:
            on_step(f"Persisting {index}/{total}: {item.ticket_name}…")
        review = QATicketReview(
            ticket_id=item.ticket_id, ticket_name=item.ticket_name,
            observation=item.observation, severity=item.severity,
            route=item.route, status_code=item.status_code,
            http_error=item.http_error, screenshot_path=item.screenshot_path,
        )
        try:
            finding = persist_review(
                review, project=item.project, clickup_list_id=item.clickup_list_id, store=store,
                pass_status=item.pass_status, fail_status=item.fail_status, on_step=on_step,
            )
            results.append(BulkCommitResultOut(ticket_id=item.ticket_id, finding=finding))
        except ValueError as exc:
            results.append(BulkCommitResultOut(ticket_id=item.ticket_id, error=str(exc)))
    return BulkCommitOut(results=results)
