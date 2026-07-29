"""Linear operations for the QA, ticket and webapp layers.

Thin wrapper over `meta_harness.trackers.linear`, deliberately mirroring
clickup_bridge.py's function shapes so those layers can treat the two
trackers interchangeably.
"""

from __future__ import annotations

from typing import List, Optional

import requests

from meta_harness.trackers.config import LinearConfig
from meta_harness.trackers.linear import LINEAR_PRIORITY, LinearAPIError, LinearClient

LINEAR_PRIORITY_WORDS = tuple(LINEAR_PRIORITY)


class LinearIssueError(RuntimeError):
    """Raised when a write-oriented Linear call fails."""


class LinearReadError(RuntimeError):
    """Raised when a read-only Linear call fails or returns unusable output."""


def _client() -> LinearClient:
    return LinearClient(LinearConfig.from_env().linear_api_key)


def _call(description: str, call, error_cls):
    """Run a client call, reporting API, network and credential failures as `error_cls`."""
    try:
        return call(_client())
    except (LinearAPIError, requests.RequestException, RuntimeError) as exc:
        raise error_cls(f"Linear {description} failed: {exc}") from exc


def _read(description: str, call):
    return _call(description, call, LinearReadError)


def _write(description: str, call):
    return _call(description, call, LinearIssueError)


def get_linear_viewer() -> dict:
    """The authenticated user and organization — a cheap credential check."""
    payload = _read("viewer lookup", lambda c: c.get_viewer())
    if not isinstance(payload, dict):
        raise LinearReadError(f"Expected a JSON object for the viewer, got: {payload!r}")
    return payload


def list_linear_teams() -> List[dict]:
    payload = _read("team listing", lambda c: c.get_teams())
    if not isinstance(payload, list):
        raise LinearReadError(f"Expected a JSON array of teams, got: {payload!r}")
    return payload


def list_linear_states(team_id: str) -> List[dict]:
    payload = _read("state listing", lambda c: c.get_team_states(team_id))
    if not isinstance(payload, list):
        raise LinearReadError(f"Expected a JSON array of states, got: {payload!r}")
    return payload


def list_linear_members(team_id: str) -> List[dict]:
    payload = _read("member listing", lambda c: c.get_team_members(team_id))
    if not isinstance(payload, list):
        raise LinearReadError(f"Expected a JSON array of members, got: {payload!r}")
    return payload


def list_linear_projects(team_id: str) -> List[dict]:
    payload = _read("project listing", lambda c: c.get_projects(team_id))
    if not isinstance(payload, list):
        raise LinearReadError(f"Expected a JSON array of projects, got: {payload!r}")
    return payload


def list_linear_issues(team_id: str, *, limit: Optional[int] = None) -> List[dict]:
    """Every issue on the team. `limit` caps the total; omitting it fetches
    all of them (the client walks Linear's cursor pagination)."""
    payload = _read("issue listing", lambda c: c.get_issues(team_id, limit=limit))
    if not isinstance(payload, list):
        raise LinearReadError(f"Expected a JSON array of issues, got: {payload!r}")
    return payload


def get_linear_issue(issue_id: str) -> dict:
    payload = _read(f"fetch of issue {issue_id}", lambda c: c.get_issue(issue_id))
    if not isinstance(payload, dict):
        raise LinearReadError(f"Expected a JSON object for issue {issue_id}, got: {payload!r}")
    return payload


def create_linear_issue(
    team_id: str,
    title: str,
    description: str,
    *,
    priority: Optional[str] = None,
    assignee_id: Optional[str] = None,
    due_date: Optional[str] = None,
    project_id: Optional[str] = None,
    parent_id: Optional[str] = None,
) -> str:
    """Create a Linear issue and return its issue id.

    `parent_id` nests the new issue under an existing one as a sub-issue,
    which is how a generated parent/subticket hierarchy is materialized."""
    if priority is not None and priority not in LINEAR_PRIORITY_WORDS:
        raise ValueError(f"priority must be one of {LINEAR_PRIORITY_WORDS}, got {priority!r}")

    payload = _write(
        "issue creation",
        lambda c: c.create_issue(
            team_id,
            title,
            description,
            priority=LINEAR_PRIORITY[priority] if priority else None,
            assignee_id=assignee_id,
            due_date=due_date,
            project_id=project_id,
            parent_id=parent_id,
        ),
    )
    if not isinstance(payload, dict):
        raise LinearIssueError(f"Expected a JSON object for the created issue, got: {payload!r}")
    issue_id = payload.get("id")
    if not issue_id:
        raise LinearIssueError(f"Linear response missing 'id': {payload}")
    return str(issue_id)


def update_linear_issue_state(issue_id: str, state_id: str) -> dict:
    """Move a Linear issue to a different workflow state."""
    payload = _write(
        "issue state update", lambda c: c.update_issue_state(issue_id, state_id)
    )
    if not isinstance(payload, dict):
        raise LinearIssueError(f"Expected a JSON object for the updated issue, got: {payload!r}")
    return payload


def update_linear_issue(
    issue_id: str, *, title: Optional[str] = None, description: Optional[str] = None
) -> dict:
    """Edit an existing issue's title and/or description. Fields left as
    None are not sent, so nothing is blanked out by accident."""
    if title is None and description is None:
        raise ValueError("Provide a title and/or a description to update")

    payload = _write(
        "issue update", lambda c: c.update_issue(issue_id, title=title, description=description)
    )
    if not isinstance(payload, dict):
        raise LinearIssueError(f"Expected a JSON object for the updated issue, got: {payload!r}")
    return payload
