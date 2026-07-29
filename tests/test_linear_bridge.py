import pytest
import requests

from meta_harness.linear_bridge import (
    LinearIssueError,
    LinearReadError,
    create_linear_issue,
    get_linear_issue,
    get_linear_viewer,
    list_linear_issues,
    list_linear_members,
    list_linear_projects,
    list_linear_states,
    list_linear_teams,
    update_linear_issue,
    update_linear_issue_state,
)
from meta_harness.trackers.linear import LinearAPIError


class FakeClient:
    """Records the client calls the bridge makes and returns canned payloads."""

    def __init__(self, payload=None, raises=None):
        self.payload = payload
        self.raises = raises
        self.calls = []

    def _record(self, method, *args, **kwargs):
        self.calls.append((method, args, kwargs))
        if self.raises is not None:
            raise self.raises
        return self.payload

    def get_viewer(self):
        return self._record("get_viewer")

    def get_teams(self):
        return self._record("get_teams")

    def get_team_states(self, team_id):
        return self._record("get_team_states", team_id)

    def get_team_members(self, team_id):
        return self._record("get_team_members", team_id)

    def get_projects(self, team_id):
        return self._record("get_projects", team_id)

    def get_issues(self, team_id, limit=None):
        return self._record("get_issues", team_id, limit=limit)

    def get_issue(self, issue_id):
        return self._record("get_issue", issue_id)

    def create_issue(self, team_id, title, description=None, **fields):
        return self._record("create_issue", team_id, title, description, **fields)

    def update_issue_state(self, issue_id, state_id):
        return self._record("update_issue_state", issue_id, state_id)

    def update_issue(self, issue_id, *, title=None, description=None):
        return self._record("update_issue", issue_id, title=title, description=description)


@pytest.fixture
def fake(monkeypatch):
    """Install a FakeClient the tests can configure and inspect."""

    def install(payload=None, raises=None):
        client = FakeClient(payload=payload, raises=raises)
        monkeypatch.setattr("meta_harness.linear_bridge._client", lambda: client)
        return client

    return install


@pytest.fixture(autouse=True)
def linear_env(monkeypatch):
    """A predictable key so tests never depend on a real .env."""
    monkeypatch.setenv("LINEAR_API_KEY", "test-key")


def test_get_linear_viewer(fake):
    client = fake(payload={"viewer": {"id": "U1", "name": "Me"}})

    viewer = get_linear_viewer()

    assert viewer["viewer"]["name"] == "Me"
    assert client.calls[0][0] == "get_viewer"


def test_list_linear_teams(fake):
    client = fake(payload=[{"id": "T1", "name": "Team 1", "key": "TEA"}])

    teams = list_linear_teams()

    assert teams == [{"id": "T1", "name": "Team 1", "key": "TEA"}]
    assert client.calls[0][0] == "get_teams"


def test_list_linear_states_passes_team_id(fake):
    client = fake(payload=[{"id": "S1", "name": "Todo", "type": "unstarted"}])

    states = list_linear_states("T1")

    assert states[0]["name"] == "Todo"
    assert client.calls[0] == ("get_team_states", ("T1",), {})


def test_list_linear_members(fake):
    client = fake(payload=[{"id": "U1", "name": "Dev", "email": "dev@example.com"}])

    members = list_linear_members("T1")

    assert members[0]["email"] == "dev@example.com"
    assert client.calls[0] == ("get_team_members", ("T1",), {})


def test_list_linear_projects(fake):
    client = fake(payload=[{"id": "P1", "name": "Q3"}])

    projects = list_linear_projects("T1")

    assert projects[0]["name"] == "Q3"
    assert client.calls[0] == ("get_projects", ("T1",), {})


def test_list_linear_issues_passes_limit(fake):
    client = fake(payload=[{"id": "I1", "identifier": "TEA-1"}])

    list_linear_issues("T1", limit=5)

    assert client.calls[0][2] == {"limit": 5}


def test_list_linear_issues_fetches_everything_by_default(fake):
    client = fake(payload=[{"id": "I1"}])

    list_linear_issues("T1")

    assert client.calls[0][2] == {"limit": None}


def test_get_linear_issue(fake):
    client = fake(payload={"id": "I1", "identifier": "TEA-1", "title": "Fix it"})

    issue = get_linear_issue("I1")

    assert issue["title"] == "Fix it"
    assert client.calls[0] == ("get_issue", ("I1",), {})


def test_get_linear_issue_non_object_raises(fake):
    fake(payload=["not", "an", "object"])

    with pytest.raises(LinearReadError, match="Expected a JSON object"):
        get_linear_issue("I1")


def test_create_linear_issue_returns_id(fake):
    client = fake(payload={"id": "I1", "identifier": "TEA-1"})

    issue_id = create_linear_issue("T1", "Title", "Description")

    assert issue_id == "I1"
    method, args, _ = client.calls[0]
    assert method == "create_issue"
    assert args == ("T1", "Title", "Description")


def test_create_linear_issue_passes_optional_fields(fake):
    client = fake(payload={"id": "I1"})

    create_linear_issue(
        "T1",
        "Title",
        "Description",
        priority="high",
        assignee_id="U1",
        due_date="2026-08-24",
        project_id="P1",
        parent_id="I0",
    )

    fields = client.calls[0][2]
    assert fields["priority"] == 2
    assert fields["assignee_id"] == "U1"
    assert fields["due_date"] == "2026-08-24"
    assert fields["project_id"] == "P1"
    assert fields["parent_id"] == "I0"


def test_create_linear_issue_omits_optional_fields_when_absent(fake):
    client = fake(payload={"id": "I1"})

    create_linear_issue("T1", "Title", "Description")

    fields = client.calls[0][2]
    assert fields["priority"] is None
    assert fields["assignee_id"] is None
    assert fields["parent_id"] is None


def test_create_linear_issue_rejects_invalid_priority():
    with pytest.raises(ValueError, match="priority must be one of"):
        create_linear_issue("T1", "Title", "Description", priority="urgentish")


def test_create_linear_issue_raises_on_api_error(fake):
    fake(raises=LinearAPIError("Linear GraphQL error: boom"))

    with pytest.raises(LinearIssueError, match="boom"):
        create_linear_issue("T1", "Title", "Description")


def test_create_linear_issue_raises_when_id_missing(fake):
    fake(payload={"identifier": "TEA-1"})

    with pytest.raises(LinearIssueError, match="missing 'id'"):
        create_linear_issue("T1", "Title", "Description")


def test_update_linear_issue_state(fake):
    client = fake(payload={"id": "I1", "state": {"id": "S2", "name": "Done"}})

    payload = update_linear_issue_state("I1", "S2")

    assert payload["state"]["name"] == "Done"
    assert client.calls[0] == ("update_issue_state", ("I1", "S2"), {})


def test_update_linear_issue_state_raises_on_api_error(fake):
    fake(raises=LinearAPIError("state not found"))

    with pytest.raises(LinearIssueError, match="state not found"):
        update_linear_issue_state("I1", "nope")


def test_update_linear_issue_sends_only_given_fields(fake):
    client = fake(payload={"id": "I1"})

    update_linear_issue("I1", title="new title")

    assert client.calls[0][2] == {"title": "new title", "description": None}


def test_update_linear_issue_requires_a_field():
    with pytest.raises(ValueError, match="Provide a title and/or a description"):
        update_linear_issue("I1")


def test_read_error_when_payload_is_not_a_list(fake):
    fake(payload={"not": "a list"})

    with pytest.raises(LinearReadError, match="Expected a JSON array"):
        list_linear_teams()


def test_read_error_on_network_failure(fake):
    fake(raises=requests.ConnectionError("no route to host"))

    with pytest.raises(LinearReadError, match="no route to host"):
        list_linear_teams()


def test_read_error_without_credentials(monkeypatch):
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)

    with pytest.raises(LinearReadError, match="LINEAR_API_KEY is not set"):
        list_linear_teams()
