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
from typing import Optional

from meta_harness.playbook import load_agent_playbook, resolve_project_path


class ClickUpTicketError(RuntimeError):
    """Raised when the p-harness ClickUp CLI fails or returns unusable output."""


def _harness_executable(project_path: Path) -> Path:
    return project_path / ".venv" / "bin" / "harness"


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
