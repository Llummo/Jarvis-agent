"""The first real "QA flow": fetch a ClickUp ticket, have Claude analyze
it, and produce a QA finding.

Dry-run by default — analysis never persists a finding or creates a
ClickUp ticket unless explicitly asked to. Every review (dry-run or
persisted) is recorded via the same RunArchive/RunRecord mechanism the
playbook system uses for replay, so a specific review can be re-run
later against the same ticket.
"""

from __future__ import annotations

from meta_harness.claude_output import assert_usable

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

OnStep = Optional[Callable[[str], None]]


def _report(on_step: OnStep, message: str) -> None:
    if on_step is not None:
        on_step(message)

from meta_harness.clickup_bridge import ClickUpReadError, ClickUpTicketError, get_clickup_task, update_clickup_task_status
from meta_harness.linear_bridge import LinearIssueError, LinearReadError, get_linear_issue, update_linear_issue_state
from meta_harness.mcp_server.cdp_screenshot import (
    ChromiumNotFoundError,
    ScreenshotCaptureError,
    capture_screenshot,
    default_screenshot_path,
)
from meta_harness.project_config import ProjectConfigStore
from meta_harness.qa_findings import SEVERITIES, QAFinding, QAFindingStore, report_qa_issue
from meta_harness.run_archive import RunArchive, RunRecord, RunStepRecord

TRACKERS = ("clickup", "linear")
ROUTE_CHECK_TIMEOUT_S = 15.0

# Which severities count as "QA passed" for the purposes of deciding whether
# to move the ClickUp ticket's status — a minor finding (or none at all) is
# treated as passing; major/critical block the move since the ticket needs
# real correction first.
PASSING_SEVERITIES = ("minor",)


def review_passed(severity: str) -> bool:
    return severity in PASSING_SEVERITIES

CLAUDE_PATH_ENV_VAR = "META_HARNESS_CLAUDE_PATH"
CLAUDE_TIMEOUT_ENV_VAR = "META_HARNESS_QA_REVIEW_TIMEOUT_S"
DEFAULT_TIMEOUT_S = 120.0
MAX_ATTEMPTS = 3

REVIEW_AGENT_NAME = "qa_ticket_review"

REVIEW_PROMPT_TEMPLATE = (
    "You are triaging a QA ticket. Given the ticket title and description below, "
    "produce a JSON object with exactly these fields: "
    '"observation" (string, a concise summary of what needs to be checked or verified), '
    '"severity" (one of "minor", "major", "critical"), '
    '"route" (string or null — your best guess at the relative URL path this ticket is '
    'about, e.g. "/login" or "/checkout/cart"; null if the ticket gives no indication of a '
    "specific page or endpoint). "
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
    """The outcome of analyzing one ticket — not yet persisted anywhere.

    route/status_code/http_error/screenshot_path are only populated when a
    base URL is configured for the project (see project_config.py) and
    Claude inferred a route — the harness's one piece of real, executed
    evidence, rather than Claude's text-only guess.
    """

    ticket_id: str
    ticket_name: str
    observation: str
    severity: str
    route: Optional[str] = None
    status_code: Optional[int] = None
    http_error: Optional[str] = None
    screenshot_path: Optional[str] = None


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

    route = payload.get("route")
    if route is not None and not isinstance(route, str):
        raise ReviewParseError(f"Response has invalid 'route' {route!r}; must be a string or null")

    return QATicketReview(
        ticket_id=ticket_id, ticket_name=ticket_name, observation=observation.strip(), severity=severity,
        route=route.strip() if isinstance(route, str) and route.strip() else None,
    )


def analyze_ticket(
    ticket_id: str,
    ticket_name: str,
    ticket_description: str,
    *,
    timeout_s: Optional[float] = None,
    max_attempts: int = MAX_ATTEMPTS,
    on_step: OnStep = None,
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
            _report(
                on_step,
                f"Claude's response didn't pass validation ({last_error}) — "
                f"asking it to fix and retrying (attempt {attempt}/{max_attempts})…",
            )
        else:
            _report(on_step, "Claude is analyzing the ticket…")
        try:
            completed = subprocess.run(
                [claude_path, "-p", prompt, "--output-format", "text"],
                capture_output=True, text=True, timeout=resolved_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise ReviewGenerationError(f"Claude CLI timed out after {resolved_timeout}s") from exc

        if completed.returncode != 0:
            raise ReviewGenerationError(f"Claude CLI failed ({completed.returncode}): {completed.stderr.strip()}")

        _report(on_step, "Received Claude's response — validating…")
        try:
            return _parse_review(
                assert_usable(completed.stdout, action="the QA review"), ticket_id, ticket_name
            )
        except ReviewParseError as exc:
            last_error = str(exc)
            if attempt == max_attempts:
                raise
    raise AssertionError("unreachable")  # loop always returns or raises by the final attempt



def _capture_with_session(base_url: str, url: str) -> Tuple[Optional[str], bool]:
    """Capture `url`, installing a session first when one is configured.

    Returns the screenshot path and whether the page turned out to be the login
    screen.

    The login check runs whether or not a token exists. Skipping it when none
    is configured — which is exactly when the app *will* show the login — files
    a screenshot of a sign-in form as if it were the feature under test, which
    is the failure this function was written to stop.
    """
    from meta_harness.qa.auth import authenticate, looks_like_login, token_for
    from meta_harness.qa.browser import Browser, BrowserError

    token = token_for("local") or token_for("qa")
    try:
        with Browser() as browser:
            # Vía authenticate, no en línea: la sesión es una cookie httpOnly
            # y duplicar el procedimiento aquí ya dejó este camino sin sesión
            # cuando el otro sí la instalaba.
            authenticate(browser, base_url, token)
            browser.goto(url)
            saw_login = looks_like_login(browser.text_of())
            path = default_screenshot_path()
            browser.screenshot(path)
            return str(path), saw_login
    except BrowserError as exc:
        raise ScreenshotCaptureError(str(exc)) from exc


def perform_route_check(
    base_url: str, route: str, *, timeout_s: float = ROUTE_CHECK_TIMEOUT_S, on_step: OnStep = None
) -> Tuple[Optional[int], Optional[str], Optional[str]]:
    """Actually hit base_url+route and capture a screenshot — the harness's
    one piece of real, executed evidence, rather than Claude's guess from
    reading the ticket text.

    Never raises: a broken target must not abort the review, it just shows
    up as an http_error (and/or a missing screenshot) on the finding.
    """
    url = base_url.rstrip("/") + "/" + route.lstrip("/")

    _report(on_step, f"Checking {url}…")
    status_code: Optional[int] = None
    http_error: Optional[str] = None
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "meta-harness-qa/1.0"})
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            status_code = response.status
        _report(on_step, f"Got HTTP {status_code} from {url}.")
    except urllib.error.HTTPError as exc:
        status_code = exc.code
        http_error = f"HTTP {exc.code}: {exc.reason}"
        _report(on_step, f"Got HTTP {exc.code} from {url}.")
    except urllib.error.URLError as exc:
        http_error = f"Could not reach {url}: {exc.reason}"
        _report(on_step, http_error)
    except Exception as exc:  # noqa: BLE001 - a broken target must not abort the review
        http_error = f"Request to {url} failed: {exc}"
        _report(on_step, http_error)

    # An SPA answers 200 for every path — it serves the same shell and routes
    # in the browser. So the status code says the server is up, and nothing
    # about whether this route exists. What the page actually rendered is the
    # only evidence worth keeping.
    _report(on_step, f"Capturing screenshot of {url}…")
    screenshot_path: Optional[str] = None
    try:
        screenshot_path, saw_login = _capture_with_session(base_url, url)
        if saw_login:
            # Without this the review files a screenshot of a login form as if
            # it were the feature under test.
            from meta_harness.qa.auth import token_for as _token_for

            causa = (
                "no hay token configurado"
                if not (_token_for("local") or _token_for("qa"))
                else "el token configurado no es válido o expiró"
            )
            not_signed_in = (
                f"No autenticado ({causa}): la aplicación mostró la pantalla de inicio de "
                "sesión, así que la captura no corresponde a la ruta pedida. "
                "Defina SIGO_LOCAL_TOKEN con el valor de localStorage['token'] de una "
                "sesión abierta en Sigo."
            )
            http_error = f"{http_error} · {not_signed_in}" if http_error else not_signed_in
            _report(on_step, not_signed_in)
        else:
            _report(on_step, "Screenshot captured.")
    except (ChromiumNotFoundError, ScreenshotCaptureError) as exc:
        _report(on_step, f"Could not capture screenshot: {exc}")

    return status_code, http_error, screenshot_path


def _validate_tracker(tracker: str) -> None:
    if tracker not in TRACKERS:
        raise ValueError(f"Invalid tracker '{tracker}'; must be one of {TRACKERS}")


def _fetch_ticket(ticket_id: str, tracker: str) -> Tuple[str, str]:
    """Fetch a ticket/issue from whichever tracker, normalized to
    (name, description) — the shape review_qa_ticket needs regardless of
    where it came from."""
    if tracker == "linear":
        issue = get_linear_issue(ticket_id)
        return issue.get("title") or ticket_id, issue.get("description") or ""
    ticket = get_clickup_task(ticket_id)
    return ticket.get("name") or ticket_id, ticket.get("text_content") or ticket.get("description") or ""


def _move_status(ticket_id: str, tracker: str, target: str) -> None:
    """Move a ticket/issue to a new status/state. `target` is a ClickUp
    status name for tracker="clickup", or a Linear workflow state id for
    tracker="linear" — the two trackers' own vocabularies, unchanged."""
    if tracker == "linear":
        update_linear_issue_state(ticket_id, target)
    else:
        update_clickup_task_status(ticket_id, target)


def review_qa_ticket(
    ticket_id: str,
    *,
    project: str,
    tracker: str = "clickup",
    clickup_list_id: Optional[str] = None,
    linear_team_id: Optional[str] = None,
    persist: bool = False,
    store: Optional[QAFindingStore] = None,
    on_step: OnStep = None,
    pass_status: Optional[str] = None,
    fail_status: Optional[str] = None,
    project_config: Optional[ProjectConfigStore] = None,
) -> Tuple[QATicketReview, Optional[QAFinding]]:
    """Fetch a ticket/issue from `tracker` ("clickup" or "linear") and
    analyze it. Dry-run by default (persist=False): returns the review
    without saving anything. With persist=True, also reports it as a real
    QA finding (which, if critical, auto-escalates to a linked correction
    ticket in the same tracker the same way manual reporting does), and —
    if pass_status/fail_status are given — moves the ticket's status/state
    based on the outcome (see persist_review).

    If the project has a base URL configured (project_config.py) and
    Claude's review inferred a route, also performs a real HTTP check and
    screenshot capture against that URL — see perform_route_check.
    """
    _validate_tracker(tracker)
    _report(on_step, f"Fetching ticket {ticket_id} from {'Linear' if tracker == 'linear' else 'ClickUp'}…")
    ticket_name, ticket_description = _fetch_ticket(ticket_id, tracker)
    _report(on_step, f'Ticket fetched: "{ticket_name}".')

    review = analyze_ticket(ticket_id, ticket_name, ticket_description, on_step=on_step)

    config = project_config if project_config is not None else ProjectConfigStore()
    base_url = config.get_base_url(project)
    if base_url and review.route:
        status_code, http_error, screenshot_path = perform_route_check(base_url, review.route, on_step=on_step)
        review = replace(review, status_code=status_code, http_error=http_error, screenshot_path=screenshot_path)

    finding = None
    if persist:
        _report(on_step, "Saving finding to the database…")
        finding = persist_review(
            review, project=project, tracker=tracker, clickup_list_id=clickup_list_id,
            linear_team_id=linear_team_id, store=store,
            pass_status=pass_status, fail_status=fail_status, on_step=on_step,
        )
    return review, finding


def persist_review(
    review: QATicketReview,
    *,
    project: str,
    tracker: str = "clickup",
    clickup_list_id: Optional[str] = None,
    linear_team_id: Optional[str] = None,
    store: Optional[QAFindingStore] = None,
    pass_status: Optional[str] = None,
    fail_status: Optional[str] = None,
    on_step: OnStep = None,
) -> QAFinding:
    """Report an already-computed review as a real finding, without
    re-fetching or re-analyzing the ticket — what you saw in the dry-run
    is exactly what gets saved, not a fresh (possibly different) analysis.

    If pass_status/fail_status are given, also moves the ticket/issue to
    the status/state matching the review's outcome (severity "minor"
    counts as a pass, "major"/"critical" as a fail — see review_passed) —
    this is the decision the harness makes automatically once a review is
    persisted. `target` must be a ClickUp status name for tracker="clickup"
    or a Linear workflow state id for tracker="linear". A failed status
    move never rolls back the finding, which is already saved by this
    point; it's reported via on_step instead.
    """
    _validate_tracker(tracker)
    finding = report_qa_issue(
        project, review.ticket_name, review.observation, review.severity,
        tracker=tracker, clickup_list_id=clickup_list_id, linear_team_id=linear_team_id, store=store,
        screenshot_path=review.screenshot_path, checked_route=review.route,
        status_code=review.status_code, http_error=review.http_error,
    )

    target_status = pass_status if review_passed(review.severity) else fail_status
    if target_status:
        tracker_label = "Linear issue" if tracker == "linear" else "ClickUp ticket"
        outcome = "passed" if review_passed(review.severity) else "failed"
        _report(
            on_step,
            f'QA {outcome} (severity: {review.severity}) — moving {tracker_label} to "{target_status}"…',
        )
        try:
            _move_status(review.ticket_id, tracker, target_status)
            _report(on_step, f'Moved {tracker_label} to "{target_status}".')
        except (ClickUpTicketError, LinearIssueError) as exc:
            _report(on_step, f'Finding persisted, but could not move {tracker_label} to "{target_status}": {exc}')

    return finding


def _record_review(
    ticket_id: str,
    project: str,
    tracker: str,
    review: QATicketReview,
    finding: Optional[QAFinding],
    persist: bool,
    archive: RunArchive,
) -> RunRecord:
    payload = {
        "project": project,
        "tracker": tracker,
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
    tracker: str = "clickup",
    clickup_list_id: Optional[str] = None,
    linear_team_id: Optional[str] = None,
    persist: bool = False,
    store: Optional[QAFindingStore] = None,
    archive: Optional[RunArchive] = None,
    on_step: OnStep = None,
    pass_status: Optional[str] = None,
    fail_status: Optional[str] = None,
    project_config: Optional[ProjectConfigStore] = None,
) -> Tuple[QATicketReview, Optional[QAFinding], RunRecord]:
    """Review a ticket/issue and archive the result so it can be replayed later."""
    review, finding = review_qa_ticket(
        ticket_id, project=project, tracker=tracker, clickup_list_id=clickup_list_id,
        linear_team_id=linear_team_id,
        persist=persist, store=store, on_step=on_step,
        pass_status=pass_status, fail_status=fail_status, project_config=project_config,
    )
    archive = archive if archive is not None else RunArchive(REVIEW_AGENT_NAME)
    record = _record_review(ticket_id, project, tracker, review, finding, persist, archive)
    return review, finding, record


def replay_qa_review(
    run_id: str,
    *,
    persist: bool = False,
    clickup_list_id: Optional[str] = None,
    linear_team_id: Optional[str] = None,
    store: Optional[QAFindingStore] = None,
    archive: Optional[RunArchive] = None,
    on_step: OnStep = None,
    pass_status: Optional[str] = None,
    fail_status: Optional[str] = None,
    project_config: Optional[ProjectConfigStore] = None,
) -> Tuple[RunRecord, QATicketReview, Optional[QAFinding], RunRecord]:
    """Re-run a previously recorded ticket review against the same ticket/issue.

    Dry-run by default (persist=False), matching review_qa_ticket's
    default — replaying repeatedly must not silently pile up duplicate
    findings/correction tickets. Pass persist=True to report the replayed
    result for real. The tracker is read back from the original recorded
    run, same as the project, so a replay always targets the same tracker
    it was originally reviewed against.
    """
    archive = archive if archive is not None else RunArchive(REVIEW_AGENT_NAME)
    _report(on_step, f"Loading recorded run {run_id}…")
    original = archive.get(run_id)
    original_payload = json.loads(original.steps[0].stdout) if original.steps else {}
    project = original_payload.get("project", "unknown")
    tracker = original_payload.get("tracker", "clickup")

    review, finding, new_record = review_and_record(
        original.subject_id, project=project, tracker=tracker, clickup_list_id=clickup_list_id,
        linear_team_id=linear_team_id,
        persist=persist, store=store, archive=archive, on_step=on_step,
        pass_status=pass_status, fail_status=fail_status, project_config=project_config,
    )
    return original, review, finding, new_record


def review_tickets_bulk(
    ticket_ids: Sequence[str],
    *,
    project: str,
    tracker: str = "clickup",
    clickup_list_id: Optional[str] = None,
    linear_team_id: Optional[str] = None,
    on_step: OnStep = None,
    project_config: Optional[ProjectConfigStore] = None,
) -> List[Tuple[str, Optional[QATicketReview], Optional[str]]]:
    """Dry-run QA analysis across many tickets/issues at once.

    Never persists anything — matches review_qa_ticket's own dry-run
    default; the caller reviews the batch and commits the ones it wants
    via persist_review/the /bulk/commit route. One ticket failing (a bad
    id, a Claude hiccup) doesn't stop the rest, same as bulk ticket
    creation's per-item error isolation.

    Returns a list of (ticket_id, review-or-None, error-or-None) in the
    same order as ticket_ids.
    """
    results: List[Tuple[str, Optional[QATicketReview], Optional[str]]] = []
    total = len(ticket_ids)
    for index, ticket_id in enumerate(ticket_ids, start=1):
        _report(on_step, f"Reviewing ticket {index}/{total}: {ticket_id}…")
        try:
            review, _finding = review_qa_ticket(
                ticket_id, project=project, tracker=tracker, clickup_list_id=clickup_list_id,
                linear_team_id=linear_team_id,
                persist=False, on_step=on_step, project_config=project_config,
            )
            results.append((ticket_id, review, None))
        except (ClickUpReadError, LinearReadError, QAFlowError) as exc:
            results.append((ticket_id, None, str(exc)))
    return results
