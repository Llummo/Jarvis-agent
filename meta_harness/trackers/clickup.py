"""ClickUp REST API client."""

from __future__ import annotations

import requests

API_BASE = "https://api.clickup.com/api/v2"

# ClickUp's native priority field is an integer: 1=urgent ... 4=low.
CLICKUP_PRIORITY = {"urgent": 1, "high": 2, "normal": 3, "low": 4}


class ClickUpAPIError(RuntimeError):
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self.payload = payload
        super().__init__(f"ClickUp API error {status_code}: {payload}")


class ClickUpClient:
    def __init__(self, api_token: str, base_url: str = API_BASE):
        self._base_url = base_url.rstrip("/")
        self._session = requests.Session()
        # ClickUp personal API tokens go directly in the Authorization
        # header, unlike OAuth tokens which use a "Bearer " prefix.
        self._session.headers.update(
            {"Authorization": api_token, "Content-Type": "application/json"}
        )

    def _request(self, method: str, path: str, **kwargs) -> dict:
        response = self._session.request(method, f"{self._base_url}{path}", **kwargs)
        if not response.ok:
            try:
                payload = response.json()
            except ValueError:
                # Not every ClickUp failure answers with JSON — a bad path or a
                # gateway error returns HTML. Reporting the status code beats
                # masking it behind a decode error from the error handler itself.
                payload = {"error": response.text[:500]}
            raise ClickUpAPIError(response.status_code, payload)
        return response.json()

    def get_teams(self) -> list[dict]:
        return self._request("GET", "/team")["teams"]

    def get_spaces(self, team_id: str) -> list[dict]:
        return self._request("GET", f"/team/{team_id}/space")["spaces"]

    def get_folders(self, space_id: str) -> list[dict]:
        return self._request("GET", f"/space/{space_id}/folder")["folders"]

    def get_lists(self, *, space_id: str | None = None, folder_id: str | None = None) -> list[dict]:
        if folder_id:
            return self._request("GET", f"/folder/{folder_id}/list")["lists"]
        if space_id:
            return self._request("GET", f"/space/{space_id}/list")["lists"]
        raise ValueError("Provide either space_id or folder_id")

    def get_task(self, task_id: str) -> dict:
        return self._request("GET", f"/task/{task_id}")

    def get_tasks(self, list_id: str, limit: int | None = None) -> list[dict]:
        """Every task in the list, following ClickUp's page pagination.

        ClickUp returns at most 100 tasks per page and reports whether the
        page was the last one, so a single call silently truncates a list
        of any real size. `limit` caps the total when a caller only wants
        the first N.
        """
        tasks: list[dict] = []
        page = 0
        while True:
            # subtasks=true is required or ClickUp silently omits every subtask,
            # which makes a list of parent tickets look like most of its
            # contents went missing.
            payload = self._request(
                "GET", f"/list/{list_id}/task", params={"page": page, "subtasks": "true"}
            )
            batch = payload.get("tasks") or []
            tasks.extend(batch)
            if payload.get("last_page", True) or not batch:
                break
            if limit is not None and len(tasks) >= limit:
                break
            page += 1
        return tasks[:limit] if limit is not None else tasks

    def create_task(
        self,
        list_id: str,
        name: str,
        description: str | None = None,
        **fields,
    ) -> dict:
        body = {"name": name, "description": description or "", **fields}
        return self._request("POST", f"/list/{list_id}/task", json=body)

    def update_task_status(self, task_id: str, status: str) -> dict:
        return self._request("PUT", f"/task/{task_id}", json={"status": status})

    def update_task(
        self, task_id: str, *, name: str | None = None, description: str | None = None, **fields
    ) -> dict:
        """Edit an existing task's content. Only the fields actually given
        are sent, so this never blanks out something it wasn't asked to
        change."""
        body: dict = dict(fields)
        if name is not None:
            body["name"] = name
        if description is not None:
            body["description"] = description
        if not body:
            raise ValueError("Provide at least one field to update")
        return self._request("PUT", f"/task/{task_id}", json=body)
