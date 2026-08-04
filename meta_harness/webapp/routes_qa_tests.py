"""Endpoints for browser tests driven from a ticket.

Two steps, kept apart on purpose. `/plan` reads a ticket and writes a plan and
touches nothing else; `/run` executes a plan a human has looked at. The gap
between them is where review happens, so they must not be one call.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException

from meta_harness.clickup_bridge import ClickUpReadError
from meta_harness.linear_bridge import LinearReadError
from meta_harness.qa.authoring import AuthoringError, author_plan
from meta_harness.qa.environments import EnvironmentError_, resolve_environment
from meta_harness.qa.plan import PlanError, TestPlan
from meta_harness.qa.report import render_markdown, save_report
from meta_harness.qa.runner import run_plan
from meta_harness.qa_flow import _fetch_ticket
from meta_harness.webapp import progress
from meta_harness.webapp.schemas import QATestPlanIn, QATestRunIn

router = APIRouter()

# Evidence lives under the gitignored state directory: screenshots of a real
# app are not something to commit.
EVIDENCE_ROOT = Path(__file__).resolve().parent.parent.parent / "qa" / "test-evidence"


def _on_step(token):
    if not token:
        return None
    progress.start(token)
    return lambda message: progress.push(token, message)


@router.get("/environments")
def get_environments() -> dict:
    """Which targets are configured, and which are missing a URL."""
    from meta_harness.qa.environments import ENVIRONMENTS

    available = []
    for name in ENVIRONMENTS:
        try:
            available.append({**resolve_environment(name).describe(), "ready": True})
        except EnvironmentError_ as exc:
            available.append({"name": name, "ui_url": "", "ready": False, "reason": str(exc)})
    return {"environments": available}


@router.post("/plan")
def post_plan(body: QATestPlanIn) -> dict:
    """Write a reviewable test plan from a ticket's acceptance criteria."""
    try:
        title, description = _fetch_ticket(body.ticket_id, body.tracker)
    except (ClickUpReadError, LinearReadError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    try:
        plan = author_plan(
            body.ticket_id,
            title,
            description,
            environment=body.environment,
            on_step=_on_step(body.progress_token),
        )
    except AuthoringError as exc:
        # A ticket without a route or an endpoint is not an outage; it is a
        # ticket that cannot be tested this way, and the message says why.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PlanError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return plan.to_dict()


@router.post("/run")
def post_run(body: QATestRunIn) -> dict:
    """Execute an approved plan and return the results with evidence."""
    try:
        plan = TestPlan.from_dict(body.plan)
    except PlanError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        environment = resolve_environment(body.environment, ui_url=body.ui_url or None)
    except EnvironmentError_ as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        run = run_plan(
            plan, environment, evidence_root=EVIDENCE_ROOT, on_step=_on_step(body.progress_token)
        )
    except Exception as exc:  # noqa: BLE001 - a broken target must not 500 the UI
        raise HTTPException(status_code=502, detail=f"No se pudo ejecutar el plan: {exc}") from exc

    report_path = save_report(run)
    return {**run.to_dict(), "report_markdown": render_markdown(run), "report_path": str(report_path)}
