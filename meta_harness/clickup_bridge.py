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


CLICKUP_PRIORITY_WORDS = ("urgent", "high", "normal", "low")


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


def list_clickup_folders(space_id: str, *, project_path: Optional[Path] = None) -> List[dict]:
    payload = _run_harness_json(["clickup", "folders", "--space-id", space_id], project_path=project_path)
    if not isinstance(payload, list):
        raise ClickUpReadError(f"Expected a JSON array of folders, got: {payload!r}")
    return payload


def list_clickup_lists(
    space_id: Optional[str] = None,
    *,
    folder_id: Optional[str] = None,
    project_path: Optional[Path] = None,
) -> List[dict]:
    """List lists in a space or a folder.

    ClickUp's /space/{id}/list only returns "folderless" lists — lists
    nested inside a folder (e.g. a "Project-1" folder) need --folder-id
    instead, which is why both are supported here.
    """
    if folder_id:
        args = ["clickup", "lists", "--folder-id", folder_id]
    elif space_id:
        args = ["clickup", "lists", "--space-id", space_id]
    else:
        raise ValueError("Provide either space_id or folder_id")

    payload = _run_harness_json(args, project_path=project_path)
    if not isinstance(payload, list):
        raise ClickUpReadError(f"Expected a JSON array of lists, got: {payload!r}")
    return payload


def list_clickup_tasks(list_id: str, *, project_path: Optional[Path] = None) -> List[dict]:
    payload = _run_harness_json(["clickup", "tasks", "--list-id", list_id], project_path=project_path)
    if not isinstance(payload, list):
        raise ClickUpReadError(f"Expected a JSON array of tasks, got: {payload!r}")
    return payload


def get_clickup_task(task_id: str, *, project_path: Optional[Path] = None) -> dict:
    payload = _run_harness_json(["clickup", "get-task", "--task-id", task_id], project_path=project_path)
    if not isinstance(payload, dict):
        raise ClickUpReadError(f"Expected a JSON object for task {task_id}, got: {payload!r}")
    return payload


def create_clickup_ticket(
    name: str,
    description: str,
    *,
    list_id: Optional[str] = None,
    priority: Optional[str] = None,
    assignees: Optional[Sequence[int]] = None,
    due_date_ms: Optional[int] = None,
    parent: Optional[str] = None,
    project_path: Optional[Path] = None,
) -> str:
    """Create a ClickUp task via the p-harness CLI; return its task id.

    `parent` nests the new task under an existing one as a subtask, which
    is how a generated parent/subticket hierarchy is materialized."""
    if priority is not None and priority not in CLICKUP_PRIORITY_WORDS:
        raise ValueError(f"priority must be one of {CLICKUP_PRIORITY_WORDS}, got {priority!r}")

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
    if priority is not None:
        command += ["--priority", priority]
    if assignees:
        command += ["--assignees", ",".join(str(a) for a in assignees)]
    if due_date_ms is not None:
        command += ["--due-date", str(due_date_ms)]
    if parent:
        command += ["--parent", parent]

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


def update_clickup_task_status(
    task_id: str,
    status: str,
    *,
    project_path: Optional[Path] = None,
) -> dict:
    """Move a ClickUp task to a different status via the p-harness CLI.

    `status` must be a status name valid for that task's list (e.g. "done",
    "in progress") — ClickUp rejects anything else with a 400.
    """
    resolved_path = project_path
    if resolved_path is None:
        resolved_path = resolve_project_path(load_agent_playbook("clickup"))

    command = [
        str(_harness_executable(resolved_path)),
        "clickup",
        "set-status",
        "--task-id",
        task_id,
        "--status",
        status,
    ]
    completed = subprocess.run(command, cwd=resolved_path, capture_output=True, text=True)
    if completed.returncode != 0:
        raise ClickUpTicketError(
            f"ClickUp status update failed ({completed.returncode}): {completed.stderr.strip()}"
        )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ClickUpTicketError(f"Could not parse ClickUp CLI output: {completed.stdout!r}") from exc


def update_clickup_task(
    task_id: str,
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
    project_path: Optional[Path] = None,
) -> dict:
    """Edit an existing task's name and/or description. Fields left as
    None are not sent, so nothing is blanked out by accident."""
    if name is None and description is None:
        raise ValueError("Provide a name and/or a description to update")

    resolved_path = project_path
    if resolved_path is None:
        resolved_path = resolve_project_path(load_agent_playbook("clickup"))

    command = [str(_harness_executable(resolved_path)), "clickup", "update-task", "--task-id", task_id]
    if name is not None:
        command += ["--name", name]
    if description is not None:
        command += ["--description", description]

    completed = subprocess.run(command, cwd=resolved_path, capture_output=True, text=True)
    if completed.returncode != 0:
        raise ClickUpTicketError(
            f"ClickUp task update failed ({completed.returncode}): {completed.stderr.strip()}"
        )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ClickUpTicketError(f"Could not parse ClickUp CLI output: {completed.stdout!r}") from exc
