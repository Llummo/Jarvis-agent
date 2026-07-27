"""Shell out to the sibling p-harness Linear CLI.

Same subprocess-boundary rationale as clickup_bridge.py — meta_harness
never couples to p-harness's venv, dependencies, or Linear credentials
directly. Deliberately mirrors that module's function shapes so the
webapp/QA layers can treat the two trackers interchangeably.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import List, Optional, Sequence

from meta_harness.playbook import load_agent_playbook, resolve_project_path

LINEAR_PRIORITY_WORDS = ("urgent", "high", "normal", "low")


class LinearIssueError(RuntimeError):
    """Raised when a write-oriented p-harness Linear CLI subcommand fails."""


class LinearReadError(RuntimeError):
    """Raised when a read-only p-harness Linear CLI subcommand fails or returns unusable output."""


def _harness_executable(project_path: Path) -> Path:
    return project_path / ".venv" / "bin" / "harness"


def _resolve_path(project_path: Optional[Path]) -> Path:
    if project_path is not None:
        return project_path
    return resolve_project_path(load_agent_playbook("linear"))


def _run_harness_json(
    args: Sequence[str], *, project_path: Optional[Path] = None, error_cls=LinearReadError
) -> object:
    """Run `harness <args>` via the p-harness CLI; return parsed JSON stdout."""
    resolved_path = _resolve_path(project_path)
    command = [str(_harness_executable(resolved_path)), *args]
    completed = subprocess.run(command, cwd=resolved_path, capture_output=True, text=True)
    if completed.returncode != 0:
        raise error_cls(
            f"p-harness Linear CLI failed ({completed.returncode}) for {' '.join(args)}: "
            f"{completed.stderr.strip()}"
        )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise error_cls(
            f"Could not parse p-harness Linear CLI output for {' '.join(args)}: {completed.stdout!r}"
        ) from exc


def list_linear_teams(*, project_path: Optional[Path] = None) -> List[dict]:
    payload = _run_harness_json(["linear", "teams"], project_path=project_path)
    if not isinstance(payload, list):
        raise LinearReadError(f"Expected a JSON array of teams, got: {payload!r}")
    return payload


def list_linear_states(team_id: str, *, project_path: Optional[Path] = None) -> List[dict]:
    payload = _run_harness_json(["linear", "states", "--team-id", team_id], project_path=project_path)
    if not isinstance(payload, list):
        raise LinearReadError(f"Expected a JSON array of states, got: {payload!r}")
    return payload


def list_linear_members(team_id: str, *, project_path: Optional[Path] = None) -> List[dict]:
    payload = _run_harness_json(["linear", "members", "--team-id", team_id], project_path=project_path)
    if not isinstance(payload, list):
        raise LinearReadError(f"Expected a JSON array of members, got: {payload!r}")
    return payload


def list_linear_projects(team_id: str, *, project_path: Optional[Path] = None) -> List[dict]:
    payload = _run_harness_json(["linear", "projects", "--team-id", team_id], project_path=project_path)
    if not isinstance(payload, list):
        raise LinearReadError(f"Expected a JSON array of projects, got: {payload!r}")
    return payload


def list_linear_issues(
    team_id: str, *, limit: Optional[int] = None, project_path: Optional[Path] = None
) -> List[dict]:
    """Every issue on the team. `limit` caps the total; omitting it fetches
    all of them (the CLI walks Linear's cursor pagination)."""
    args = ["linear", "issues", "--team-id", team_id]
    if limit is not None:
        args += ["--limit", str(limit)]
    payload = _run_harness_json(args, project_path=project_path)
    if not isinstance(payload, list):
        raise LinearReadError(f"Expected a JSON array of issues, got: {payload!r}")
    return payload


def get_linear_issue(issue_id: str, *, project_path: Optional[Path] = None) -> dict:
    payload = _run_harness_json(["linear", "get-issue", "--issue-id", issue_id], project_path=project_path)
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
    project_path: Optional[Path] = None,
) -> str:
    """Create a Linear issue via the p-harness CLI; return its issue id.

    `parent_id` nests the new issue under an existing one as a sub-issue,
    which is how a generated parent/subticket hierarchy is materialized."""
    if priority is not None and priority not in LINEAR_PRIORITY_WORDS:
        raise ValueError(f"priority must be one of {LINEAR_PRIORITY_WORDS}, got {priority!r}")

    args = ["linear", "create-issue", "--team-id", team_id, "--title", title, "--description", description]
    if priority is not None:
        args += ["--priority", priority]
    if assignee_id:
        args += ["--assignee-id", assignee_id]
    if due_date:
        args += ["--due-date", due_date]
    if project_id:
        args += ["--project-id", project_id]
    if parent_id:
        args += ["--parent-id", parent_id]

    payload = _run_harness_json(args, project_path=project_path, error_cls=LinearIssueError)
    if not isinstance(payload, dict):
        raise LinearIssueError(f"Expected a JSON object for the created issue, got: {payload!r}")
    issue_id = payload.get("id")
    if not issue_id:
        raise LinearIssueError(f"Linear CLI response missing 'id': {payload}")
    return str(issue_id)


def update_linear_issue_state(
    issue_id: str, state_id: str, *, project_path: Optional[Path] = None
) -> dict:
    """Move a Linear issue to a different workflow state."""
    payload = _run_harness_json(
        ["linear", "set-state", "--issue-id", issue_id, "--state-id", state_id],
        project_path=project_path,
        error_cls=LinearIssueError,
    )
    if not isinstance(payload, dict):
        raise LinearIssueError(f"Expected a JSON object for the updated issue, got: {payload!r}")
    return payload


def update_linear_issue(
    issue_id: str,
    *,
    title: Optional[str] = None,
    description: Optional[str] = None,
    project_path: Optional[Path] = None,
) -> dict:
    """Edit an existing issue's title and/or description. Fields left as
    None are not sent, so nothing is blanked out by accident."""
    if title is None and description is None:
        raise ValueError("Provide a title and/or a description to update")

    args = ["linear", "update-issue", "--issue-id", issue_id]
    if title is not None:
        args += ["--title", title]
    if description is not None:
        args += ["--description", description]

    payload = _run_harness_json(args, project_path=project_path, error_cls=LinearIssueError)
    if not isinstance(payload, dict):
        raise LinearIssueError(f"Expected a JSON object for the updated issue, got: {payload!r}")
    return payload
