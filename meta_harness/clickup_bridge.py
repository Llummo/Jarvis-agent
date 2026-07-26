"""Shell out to the sibling p-harness ClickUp CLI to create correction
tickets for critical QA findings.

Kept as a subprocess boundary — mirrors playbook.py's existing pattern of
driving project CLIs — so meta_harness never couples to p-harness's venv,
dependencies, or ClickUp credentials directly.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import List, Optional, Sequence

from meta_harness.playbook import load_agent_playbook, resolve_project_path


class ClickUpTicketError(RuntimeError):
    """Raised when the p-harness ClickUp CLI fails or returns unusable output."""


class ClickUpReadError(RuntimeError):
    """Raised when a read-only p-harness ClickUp CLI subcommand fails or returns unusable output."""


def _harness_executable(project_path: Path) -> Path:
    return project_path / ".venv" / "bin" / "harness"


def _run_harness_json(args: Sequence[str], *, project_path: Optional[Path] = None) -> object:
    """Run `harness <args>` via the p-harness CLI; return parsed JSON stdout."""
    resolved_path = project_path
    if resolved_path is None:
        resolved_path = resolve_project_path(load_agent_playbook("clickup"))

    command = [str(_harness_executable(resolved_path)), *args]
    completed = subprocess.run(command, cwd=resolved_path, capture_output=True, text=True)
    if completed.returncode != 0:
        raise ClickUpReadError(
            f"p-harness ClickUp CLI failed ({completed.returncode}) for {' '.join(args)}: "
            f"{completed.stderr.strip()}"
        )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ClickUpReadError(
            f"Could not parse p-harness ClickUp CLI output for {' '.join(args)}: {completed.stdout!r}"
        ) from exc


def list_clickup_teams(*, project_path: Optional[Path] = None) -> List[dict]:
    payload = _run_harness_json(["clickup", "teams"], project_path=project_path)
    if not isinstance(payload, list):
        raise ClickUpReadError(f"Expected a JSON array of teams, got: {payload!r}")
    return payload


def list_clickup_spaces(team_id: str, *, project_path: Optional[Path] = None) -> List[dict]:
    payload = _run_harness_json(["clickup", "spaces", "--team-id", team_id], project_path=project_path)
    if not isinstance(payload, list):
        raise ClickUpReadError(f"Expected a JSON array of spaces, got: {payload!r}")
    return payload


def list_clickup_lists(space_id: str, *, project_path: Optional[Path] = None) -> List[dict]:
    payload = _run_harness_json(["clickup", "lists", "--space-id", space_id], project_path=project_path)
    if not isinstance(payload, list):
        raise ClickUpReadError(f"Expected a JSON array of lists, got: {payload!r}")
    return payload


def list_clickup_tasks(list_id: str, *, project_path: Optional[Path] = None) -> List[dict]:
    payload = _run_harness_json(["clickup", "tasks", "--list-id", list_id], project_path=project_path)
    if not isinstance(payload, list):
        raise ClickUpReadError(f"Expected a JSON array of tasks, got: {payload!r}")
    return payload


def create_clickup_ticket(
    name: str,
    description: str,
    *,
    list_id: Optional[str] = None,
    project_path: Optional[Path] = None,
) -> str:
    """Create a ClickUp task via the p-harness CLI; return its task id."""
    resolved_path = project_path
    if resolved_path is None:
        resolved_path = resolve_project_path(load_agent_playbook("clickup"))

    command = [
        str(_harness_executable(resolved_path)),
        "clickup",
        "create-task",
        "--name",
        name,
        "--description",
        description,
    ]
    if list_id:
        command += ["--list-id", list_id]

    completed = subprocess.run(command, cwd=resolved_path, capture_output=True, text=True)
    if completed.returncode != 0:
        raise ClickUpTicketError(
            f"ClickUp ticket creation failed ({completed.returncode}): {completed.stderr.strip()}"
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ClickUpTicketError(f"Could not parse ClickUp CLI output: {completed.stdout!r}") from exc

    task_id = payload.get("id")
    if not task_id:
        raise ClickUpTicketError(f"ClickUp CLI response missing 'id': {payload}")
    return str(task_id)
