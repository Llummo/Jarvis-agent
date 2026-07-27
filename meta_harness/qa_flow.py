"""The first real "QA flow": fetch a ClickUp ticket, have Claude analyze
it, and produce a QA finding.

Dry-run by default — analysis never persists a finding or creates a
ClickUp ticket unless explicitly asked to. Every review (dry-run or
persisted) is recorded via the same RunArchive/RunRecord mechanism the
playbook system uses for replay, so a specific review can be re-run
later against the same ticket.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

from meta_harness.clickup_bridge import get_clickup_task
from meta_harness.qa_findings import SEVERITIES, QAFinding, QAFindingStore, report_qa_issue
from meta_harness.run_archive import RunArchive, RunRecord, RunStepRecord

CLAUDE_PATH_ENV_VAR = "META_HARNESS_CLAUDE_PATH"
CLAUDE_TIMEOUT_ENV_VAR = "META_HARNESS_QA_REVIEW_TIMEOUT_S"
DEFAULT_TIMEOUT_S = 120.0
MAX_ATTEMPTS = 3

REVIEW_AGENT_NAME = "qa_ticket_review"

REVIEW_PROMPT_TEMPLATE = (
    "You are triaging a QA ticket. Given the ticket title and description below, "
    "produce a JSON object with exactly these fields: "
    '"observation" (string, a concise summary of what needs to be checked or verified), '
    '"severity" (one of "minor", "major", "critical"). '
    "Output ONLY the JSON object — no prose, no markdown code fences.\n\n"
    "Title: {title}\nDescription: {description}"
)


class QAFlowError(RuntimeError):
    """Base class for QA ticket review flow errors."""


class ClaudeNotFoundError(QAFlowError):
    """No usable `claude` CLI binary could be located."""


class ReviewGenerationError(QAFlowError):
    """Raised when invoking the `claude` CLI fails or times out."""


class ReviewParseError(QAFlowError):
    """Raised when Claude's output isn't valid JSON or doesn't match the expected shape."""


@dataclass
class QATicketReview:
    """The outcome of analyzing one ticket — not yet persisted anywhere."""

    ticket_id: str
    ticket_name: str
    observation: str
    severity: str


def _find_claude() -> str:
    """Locate the `claude` CLI binary: env var override, then PATH."""
    env_value = os.getenv(CLAUDE_PATH_ENV_VAR)
    if env_value and Path(env_value).expanduser().exists():
        return str(Path(env_value).expanduser().resolve())

    found = shutil.which("claude")
    if found:
        return found

    raise ClaudeNotFoundError(
        f"No `claude` CLI binary found on PATH. Set {CLAUDE_PATH_ENV_VAR} to its location, "
        "or install/log in to Claude Code."
    )


def _strip_code_fences(raw: str) -> str:
    stripped = raw.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1]
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
    return stripped.strip()


def _parse_review(raw_output: str, ticket_id: str, ticket_name: str) -> QATicketReview:
    cleaned = _strip_code_fences(raw_output)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ReviewParseError(f"Claude did not return valid JSON: {raw_output[:500]!r}") from exc

    if not isinstance(payload, dict):
        raise ReviewParseError(f"Expected a JSON object, got: {type(payload).__name__}")

    observation = payload.get("observation")
    if not isinstance(observation, str) or not observation.strip():
        raise ReviewParseError("Response missing a valid 'observation' string")

    severity = payload.get("severity")
    if severity not in SEVERITIES:
        raise ReviewParseError(f"Response has invalid severity {severity!r}; must be one of {SEVERITIES}")

    return QATicketReview(
        ticket_id=ticket_id, ticket_name=ticket_name, observation=observation.strip(), severity=severity
    )


def analyze_ticket(
    ticket_id: str,
    ticket_name: str,
    ticket_description: str,
    *,
    timeout_s: Optional[float] = None,
    max_attempts: int = MAX_ATTEMPTS,
) -> QATicketReview:
    """Harnessed `claude -p` call: same retry-with-repair pattern as
    ticket_generator.py — if the response fails validation, the specific
    error is fed back into a follow-up prompt before giving up.
    """
    claude_path = _find_claude()
    resolved_timeout = (
        timeout_s if timeout_s is not None else float(os.getenv(CLAUDE_TIMEOUT_ENV_VAR, DEFAULT_TIMEOUT_S))
    )
    base_prompt = REVIEW_PROMPT_TEMPLATE.format(
        title=ticket_name, description=ticket_description or "(no description)"
    )

    prompt = base_prompt
    last_error: Optional[str] = None
    for attempt in range(1, max_attempts + 1):
        if last_error is not None:
            prompt = (
                f"{base_prompt}\n\nYour previous response was invalid: {last_error} "
                "Fix this and output ONLY the corrected JSON object."
            )
        try:
            completed = subprocess.run(
                [claude_path, "-p", prompt, "--output-format", "text"],
                capture_output=True, text=True, timeout=resolved_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise ReviewGenerationError(f"Claude CLI timed out after {resolved_timeout}s") from exc

        if completed.returncode != 0:
            raise ReviewGenerationError(f"Claude CLI failed ({completed.returncode}): {completed.stderr.strip()}")

        try:
            return _parse_review(completed.stdout, ticket_id, ticket_name)
        except ReviewParseError as exc:
            last_error = str(exc)
            if attempt == max_attempts:
                raise
    raise AssertionError("unreachable")  # loop always returns or raises by the final attempt


def review_qa_ticket(
    ticket_id: str,
    *,
    project: str,
    clickup_list_id: Optional[str] = None,
    persist: bool = False,
    project_path: Optional[Path] = None,
    store: Optional[QAFindingStore] = None,
) -> Tuple[QATicketReview, Optional[QAFinding]]:
    """Fetch a ClickUp ticket and analyze it. Dry-run by default (persist=False):
    returns the review without saving anything. With persist=True, also
    reports it as a real QA finding (which, if critical, auto-escalates
    to a linked ClickUp ticket the same way manual reporting does).
    """
    ticket = get_clickup_task(ticket_id, project_path=project_path)
    ticket_name = ticket.get("name") or ticket_id
    ticket_description = ticket.get("text_content") or ticket.get("description") or ""

    review = analyze_ticket(ticket_id, ticket_name, ticket_description)

    finding = None
    if persist:
        finding = persist_review(review, project=project, clickup_list_id=clickup_list_id, store=store)
    return review, finding


def persist_review(
    review: QATicketReview,
    *,
    project: str,
    clickup_list_id: Optional[str] = None,
    store: Optional[QAFindingStore] = None,
) -> QAFinding:
    """Report an already-computed review as a real finding, without
    re-fetching or re-analyzing the ticket — what you saw in the dry-run
    is exactly what gets saved, not a fresh (possibly different) analysis.
    """
    return report_qa_issue(
        project, review.ticket_name, review.observation, review.severity,
        clickup_list_id=clickup_list_id, store=store,
    )


def _record_review(
    ticket_id: str,
    project: str,
    review: QATicketReview,
    finding: Optional[QAFinding],
    persist: bool,
    archive: RunArchive,
) -> RunRecord:
    payload = {
        "project": project,
        "observation": review.observation,
        "severity": review.severity,
        "persisted": persist,
        "finding_id": finding.id if finding else None,
    }
    record = RunRecord(
        run_id=uuid.uuid4().hex[:12],
        agent=REVIEW_AGENT_NAME,
        subject_id=ticket_id,
        started_at=datetime.now(timezone.utc).isoformat(),
        ok=True,
        steps=[RunStepRecord(command=["review", ticket_id], returncode=0, stdout=json.dumps(payload), stderr="")],
    )
    archive.append(record)
    return record


def review_and_record(
    ticket_id: str,
    *,
    project: str,
    clickup_list_id: Optional[str] = None,
    persist: bool = False,
    project_path: Optional[Path] = None,
    store: Optional[QAFindingStore] = None,
    archive: Optional[RunArchive] = None,
) -> Tuple[QATicketReview, Optional[QAFinding], RunRecord]:
    """Review a ticket and archive the result so it can be replayed later."""
    review, finding = review_qa_ticket(
        ticket_id, project=project, clickup_list_id=clickup_list_id,
        persist=persist, project_path=project_path, store=store,
    )
    archive = archive if archive is not None else RunArchive(REVIEW_AGENT_NAME)
    record = _record_review(ticket_id, project, review, finding, persist, archive)
    return review, finding, record


def replay_qa_review(
    run_id: str,
    *,
    persist: bool = False,
    clickup_list_id: Optional[str] = None,
    project_path: Optional[Path] = None,
    store: Optional[QAFindingStore] = None,
    archive: Optional[RunArchive] = None,
) -> Tuple[RunRecord, QATicketReview, Optional[QAFinding], RunRecord]:
    """Re-run a previously recorded ticket review against the same ticket.

    Dry-run by default (persist=False), matching review_qa_ticket's
    default — replaying repeatedly must not silently pile up duplicate
    findings/ClickUp tickets. Pass persist=True to report the replayed
    result for real.
    """
    archive = archive if archive is not None else RunArchive(REVIEW_AGENT_NAME)
    original = archive.get(run_id)
    original_payload = json.loads(original.steps[0].stdout) if original.steps else {}
    project = original_payload.get("project", "unknown")

    review, finding, new_record = review_and_record(
        original.subject_id, project=project, clickup_list_id=clickup_list_id,
        persist=persist, project_path=project_path, store=store, archive=archive,
    )
    return original, review, finding, new_record
