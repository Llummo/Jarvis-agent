"""ClickUp operations for the QA, ticket and webapp layers.

Thin wrapper over `meta_harness.trackers.clickup`: resolves credentials,
applies the defaults callers rely on (list id from the environment), and
translates transport-level failures into the read/write error vocabulary
the webapp routes already handle.
"""

from __future__ import annotations

from typing import List, Optional, Sequence

import requests

from meta_harness.trackers.clickup import CLICKUP_PRIORITY, ClickUpAPIError, ClickUpClient
from meta_harness.trackers.config import ClickUpConfig

CLICKUP_PRIORITY_WORDS = tuple(CLICKUP_PRIORITY)


class ClickUpTicketError(RuntimeError):
    """Raised when a write-oriented ClickUp call fails."""


class ClickUpReadError(RuntimeError):
    """Raised when a read-only ClickUp call fails or returns unusable output."""


def _client() -> ClickUpClient:
    return ClickUpClient(ClickUpConfig.from_env().clickup_api_token)


def _call(description: str, call, error_cls):
    """Run a client call, reporting API, network and credential failures as `error_cls`.

    ClickUpAPIError covers a rejected request, RequestException a network
    failure, and a bare RuntimeError a missing token — all three used to
    surface identically as a non-zero exit from the old CLI subprocess.
    """
    try:
        return call(_client())
    except (ClickUpAPIError, requests.RequestException, RuntimeError) as exc:
        raise error_cls(f"ClickUp {description} failed: {exc}") from exc


def _read(description: str, call):
    return _call(description, call, ClickUpReadError)


def _write(description: str, call):
    return _call(description, call, ClickUpTicketError)


def list_clickup_teams() -> List[dict]:
    payload = _read("team listing", lambda c: c.get_teams())
    if not isinstance(payload, list):
        raise ClickUpReadError(f"Expected a JSON array of teams, got: {payload!r}")
    return payload


def list_clickup_spaces(team_id: str) -> List[dict]:
    payload = _read("space listing", lambda c: c.get_spaces(team_id))
    if not isinstance(payload, list):
        raise ClickUpReadError(f"Expected a JSON array of spaces, got: {payload!r}")
    return payload


def list_clickup_folders(space_id: str) -> List[dict]:
    payload = _read("folder listing", lambda c: c.get_folders(space_id))
    if not isinstance(payload, list):
        raise ClickUpReadError(f"Expected a JSON array of folders, got: {payload!r}")
    return payload


def list_clickup_lists(space_id: Optional[str] = None, *, folder_id: Optional[str] = None) -> List[dict]:
    """List lists in a space or a folder.

    ClickUp's /space/{id}/list only returns "folderless" lists — lists
    nested inside a folder (e.g. a "Project-1" folder) need folder_id
    instead, which is why both are supported here.
    """
    if not folder_id and not space_id:
        raise ValueError("Provide either space_id or folder_id")

    payload = _read(
        "list listing", lambda c: c.get_lists(space_id=space_id, folder_id=folder_id)
    )
    if not isinstance(payload, list):
        raise ClickUpReadError(f"Expected a JSON array of lists, got: {payload!r}")
    return payload


def list_clickup_tasks(list_id: str) -> List[dict]:
    payload = _read("task listing", lambda c: c.get_tasks(list_id))
    if not isinstance(payload, list):
        raise ClickUpReadError(f"Expected a JSON array of tasks, got: {payload!r}")
    return payload


def get_clickup_task(task_id: str) -> dict:
    payload = _read(f"fetch of task {task_id}", lambda c: c.get_task(task_id))
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
) -> str:
    """Create a ClickUp task and return its task id.

    `parent` nests the new task under an existing one as a subtask, which
    is how a generated parent/subticket hierarchy is materialized."""
    if priority is not None and priority not in CLICKUP_PRIORITY_WORDS:
        raise ValueError(f"priority must be one of {CLICKUP_PRIORITY_WORDS}, got {priority!r}")

    fields: dict = {}
    if priority is not None:
        fields["priority"] = CLICKUP_PRIORITY[priority]
    if assignees:
        fields["assignees"] = [int(a) for a in assignees]
    if due_date_ms is not None:
        fields["due_date"] = due_date_ms
    if parent:
        fields["parent"] = parent

    try:
        target_list = list_id or ClickUpConfig.from_env().clickup_list_id
    except RuntimeError as exc:  # missing credentials
        raise ClickUpTicketError(f"ClickUp ticket creation failed: {exc}") from exc
    if not target_list:
        raise ClickUpTicketError(
            "No ClickUp list to create the task in. Pass list_id or set CLICKUP_LIST_ID in .env."
        )

    payload = _write(
        "ticket creation", lambda c: c.create_task(target_list, name, description, **fields)
    )
    if not isinstance(payload, dict):
        raise ClickUpTicketError(f"Expected a JSON object for the created task, got: {payload!r}")
    task_id = payload.get("id")
    if not task_id:
        raise ClickUpTicketError(f"ClickUp response missing 'id': {payload}")
    return str(task_id)


def update_clickup_task_status(task_id: str, status: str) -> dict:
    """Move a ClickUp task to a different status.

    `status` must be a status name valid for that task's list (e.g. "done",
    "in progress") — ClickUp rejects anything else with a 400.
    """
    payload = _write("status update", lambda c: c.update_task_status(task_id, status))
    if not isinstance(payload, dict):
        raise ClickUpTicketError(f"Expected a JSON object for the updated task, got: {payload!r}")
    return payload


def update_clickup_task(
    task_id: str, *, name: Optional[str] = None, description: Optional[str] = None
) -> dict:
    """Edit an existing task's name and/or description. Fields left as
    None are not sent, so nothing is blanked out by accident."""
    if name is None and description is None:
        raise ValueError("Provide a name and/or a description to update")

    payload = _write(
        "task update", lambda c: c.update_task(task_id, name=name, description=description)
    )
    if not isinstance(payload, dict):
        raise ClickUpTicketError(f"Expected a JSON object for the updated task, got: {payload!r}")
    return payload
