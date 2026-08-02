"""Linear GraphQL API client."""

from __future__ import annotations

import requests

API_URL = "https://api.linear.app/graphql"

# Linear's native priority field is an integer: 0=none, 1=urgent ... 4=low.
# Deliberately mirrors the ClickUp client's word-based vocabulary so the
# meta-harness layer can stay tracker-agnostic.
LINEAR_PRIORITY = {"urgent": 1, "high": 2, "normal": 3, "low": 4}


# Linear caps pagination at 250 records per request.
PAGE_SIZE = 250


class LinearAPIError(RuntimeError):
    def __init__(self, message: str, payload: dict | None = None):
        self.payload = payload
        super().__init__(message)


class LinearClient:
    def __init__(self, api_key: str, api_url: str = API_URL):
        self._api_url = api_url
        self._session = requests.Session()
        # Linear personal API keys go directly in the Authorization header,
        # with no "Bearer " prefix (OAuth tokens would use one).
        self._session.headers.update(
            {"Authorization": api_key, "Content-Type": "application/json"}
        )

    def _request(self, query: str, variables: dict | None = None) -> dict:
        response = self._session.post(
            self._api_url, json={"query": query, "variables": variables or {}}
        )
        if not response.ok:
            raise LinearAPIError(f"Linear API error {response.status_code}: {response.text}")
        payload = response.json()
        # GraphQL reports failures in a 200 body, so a non-error status is
        # not enough on its own to call the call successful.
        if payload.get("errors"):
            raise LinearAPIError(f"Linear GraphQL error: {payload['errors']}", payload)
        return payload["data"]

    def get_viewer(self) -> dict:
        query = "query { viewer { id name email } organization { id name } }"
        return self._request(query)

    def get_teams(self) -> list[dict]:
        query = "query { teams { nodes { id name key } } }"
        return self._request(query)["teams"]["nodes"]

    def get_team_states(self, team_id: str) -> list[dict]:
        query = """
        query($teamId: String!) {
          team(id: $teamId) { states { nodes { id name type } } }
        }
        """
        return self._request(query, {"teamId": team_id})["team"]["states"]["nodes"]

    def get_team_members(self, team_id: str) -> list[dict]:
        query = """
        query($teamId: String!) {
          team(id: $teamId) { members { nodes { id name email } } }
        }
        """
        return self._request(query, {"teamId": team_id})["team"]["members"]["nodes"]

    def get_projects(self, team_id: str) -> list[dict]:
        query = """
        query($teamId: String!) {
          team(id: $teamId) { projects { nodes { id name } } }
        }
        """
        return self._request(query, {"teamId": team_id})["team"]["projects"]["nodes"]

    def get_issues(self, team_id: str, limit: int | None = None) -> list[dict]:
        """Every issue on the team, following Linear's cursor pagination.

        Linear returns at most PAGE_SIZE issues per request, so a single
        call silently truncates a team of any real size — this walks
        `pageInfo.endCursor` until the last page. `limit` caps the total
        when a caller only wants the first N.
        """
        query = """
        query($teamId: String!, $first: Int!, $after: String) {
          team(id: $teamId) {
            issues(first: $first, after: $after) {
              nodes {
                id identifier title description priority dueDate
                state { id name type }
                assignee { id name email }
              }
              pageInfo { hasNextPage endCursor }
            }
          }
        }
        """
        issues: list[dict] = []
        cursor: str | None = None
        while True:
            page_size = PAGE_SIZE if limit is None else min(PAGE_SIZE, limit - len(issues))
            if page_size <= 0:
                break
            payload = self._request(query, {"teamId": team_id, "first": page_size, "after": cursor})
            page = payload["team"]["issues"]
            issues.extend(page["nodes"])
            info = page.get("pageInfo") or {}
            cursor = info.get("endCursor")
            if not info.get("hasNextPage") or not cursor:
                break
            if limit is not None and len(issues) >= limit:
                break
        # Truncate ourselves rather than trusting the server to have honored
        # `first` — the caller asked for at most `limit`.
        return issues[:limit] if limit is not None else issues

    def get_issue_index(self, team_id: str, limit: int | None = None) -> list[dict]:
        """Every issue on the team, titles only.

        Deliberately not `get_issues`: matching a pasted list of names needs
        titles and nothing else, and descriptions are the overwhelming
        majority of an issue payload. On a team of any size that difference
        is the whole cost of the request.
        """
        query = """
        query($teamId: String!, $first: Int!, $after: String) {
          team(id: $teamId) {
            issues(first: $first, after: $after) {
              nodes {
                id identifier title
                state { id name type }
                parent { id identifier }
              }
              pageInfo { hasNextPage endCursor }
            }
          }
        }
        """
        return self._paginate_issues(query, {"teamId": team_id}, limit)

    def search_issues(self, team_id: str, text: str, limit: int | None = None) -> list[dict]:
        """Issues whose title contains `text`, filtered by Linear itself.

        Used when the caller already knows roughly what it is looking for —
        picking a parent, say. Filtering server-side beats pulling the whole
        team down and discarding most of it.
        """
        query = """
        query($teamId: String!, $text: String!, $first: Int!, $after: String) {
          team(id: $teamId) {
            issues(
              first: $first
              after: $after
              filter: { title: { containsIgnoreCase: $text } }
            ) {
              nodes {
                id identifier title
                state { id name type }
                parent { id identifier }
              }
              pageInfo { hasNextPage endCursor }
            }
          }
        }
        """
        return self._paginate_issues(query, {"teamId": team_id, "text": text}, limit)

    def _paginate_issues(
        self, query: str, variables: dict, limit: int | None
    ) -> list[dict]:
        """Walk `pageInfo.endCursor` until the last page.

        Shared by every issue-listing query: Linear caps a response at
        PAGE_SIZE, so a single request silently truncates.
        """
        issues: list[dict] = []
        cursor: str | None = None
        while True:
            page_size = PAGE_SIZE if limit is None else min(PAGE_SIZE, limit - len(issues))
            if page_size <= 0:
                break
            payload = self._request(query, {**variables, "first": page_size, "after": cursor})
            page = payload["team"]["issues"]
            issues.extend(page["nodes"])
            info = page.get("pageInfo") or {}
            cursor = info.get("endCursor")
            if not info.get("hasNextPage") or not cursor:
                break
            if limit is not None and len(issues) >= limit:
                break
        return issues[:limit] if limit is not None else issues

    def get_issue(self, issue_id: str) -> dict:
        query = """
        query($issueId: String!) {
          issue(id: $issueId) {
            id identifier title description priority dueDate
            state { id name type }
            assignee { id name email }
            parent { id identifier title }
            children { nodes { id identifier title } }
          }
        }
        """
        return self._request(query, {"issueId": issue_id})["issue"]

    def create_issue(
        self,
        team_id: str,
        title: str,
        description: str | None = None,
        *,
        priority: int | None = None,
        assignee_id: str | None = None,
        due_date: str | None = None,
        project_id: str | None = None,
        parent_id: str | None = None,
    ) -> dict:
        query = """
        mutation($input: IssueCreateInput!) {
          issueCreate(input: $input) {
            success
            issue {
              id identifier title url
              state { id name }
              assignee { id name email }
              dueDate
              parent { id identifier }
            }
          }
        }
        """
        issue_input: dict = {"teamId": team_id, "title": title}
        if description is not None:
            issue_input["description"] = description
        if priority is not None:
            issue_input["priority"] = priority
        if assignee_id:
            issue_input["assigneeId"] = assignee_id
        if due_date:
            issue_input["dueDate"] = due_date
        if project_id:
            issue_input["projectId"] = project_id
        if parent_id:
            # Linear models sub-issues as a parent link on the child.
            issue_input["parentId"] = parent_id

        result = self._request(query, {"input": issue_input})["issueCreate"]
        if not result.get("success"):
            raise LinearAPIError(f"Linear issue creation failed: {result}")
        return result["issue"]

    def delete_issue(self, issue_id: str) -> dict:
        """Move an issue to Linear's trash.

        `issueDelete` is a soft delete: the issue leaves every board and query
        but stays recoverable from trash for about 30 days. That is the right
        primitive for undoing a batch — a mistake in the undo is still fixable
        from Linear's own UI, which a hard destroy would not be.
        """
        query = """
        mutation($issueId: String!) {
          issueDelete(id: $issueId) { success }
        }
        """
        return self._request(query, {"issueId": issue_id})["issueDelete"]

    def update_issue_state(self, issue_id: str, state_id: str) -> dict:
        query = """
        mutation($issueId: String!, $stateId: String!) {
          issueUpdate(id: $issueId, input: { stateId: $stateId }) {
            success
            issue { id identifier state { id name } }
          }
        }
        """
        result = self._request(query, {"issueId": issue_id, "stateId": state_id})["issueUpdate"]
        if not result.get("success"):
            raise LinearAPIError(f"Linear issue state update failed: {result}")
        return result["issue"]

    def update_issue(
        self, issue_id: str, *, title: str | None = None, description: str | None = None
    ) -> dict:
        """Edit an existing issue's content. Only the fields actually given
        are sent, so this never blanks out something it wasn't asked to
        change."""
        issue_input: dict = {}
        if title is not None:
            issue_input["title"] = title
        if description is not None:
            issue_input["description"] = description
        if not issue_input:
            raise ValueError("Provide at least one field to update")

        query = """
        mutation($issueId: String!, $input: IssueUpdateInput!) {
          issueUpdate(id: $issueId, input: $input) {
            success
            issue { id identifier title description url state { id name } }
          }
        }
        """
        result = self._request(query, {"issueId": issue_id, "input": issue_input})["issueUpdate"]
        if not result.get("success"):
            raise LinearAPIError(f"Linear issue update failed: {result}")
        return result["issue"]
