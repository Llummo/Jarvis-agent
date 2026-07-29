import pytest
import requests

from meta_harness.clickup_bridge import (
    ClickUpReadError,
    ClickUpTicketError,
    create_clickup_ticket,
    get_clickup_task,
    list_clickup_folders,
    list_clickup_lists,
    list_clickup_spaces,
    list_clickup_tasks,
    list_clickup_teams,
    update_clickup_task,
    update_clickup_task_status,
)
from meta_harness.trackers.clickup import ClickUpAPIError


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

    def get_teams(self):
        return self._record("get_teams")

    def get_spaces(self, team_id):
        return self._record("get_spaces", team_id)

    def get_folders(self, space_id):
        return self._record("get_folders", space_id)

    def get_lists(self, *, space_id=None, folder_id=None):
        return self._record("get_lists", space_id=space_id, folder_id=folder_id)

    def get_tasks(self, list_id):
        return self._record("get_tasks", list_id)

    def get_task(self, task_id):
        return self._record("get_task", task_id)

    def create_task(self, list_id, name, description=None, **fields):
        return self._record("create_task", list_id, name, description, **fields)

    def update_task_status(self, task_id, status):
        return self._record("update_task_status", task_id, status)

    def update_task(self, task_id, *, name=None, description=None, **fields):
        return self._record("update_task", task_id, name=name, description=description, **fields)


@pytest.fixture
def fake(monkeypatch):
    """Install a FakeClient the tests can configure and inspect."""

    def install(payload=None, raises=None):
        client = FakeClient(payload=payload, raises=raises)
        monkeypatch.setattr("meta_harness.clickup_bridge._client", lambda: client)
        return client

    return install


@pytest.fixture(autouse=True)
def clickup_env(monkeypatch):
    """A predictable token/list so tests never depend on a real .env."""
    monkeypatch.setenv("CLICKUP_API_TOKEN", "test-token")
    monkeypatch.setenv("CLICKUP_LIST_ID", "default-list")


def test_create_clickup_ticket_returns_task_id(fake):
    client = fake(payload={"id": "CU-123"})

    task_id = create_clickup_ticket("name", "desc", list_id="901")

    assert task_id == "CU-123"
    method, args, _ = client.calls[0]
    assert method == "create_task"
    assert args[:3] == ("901", "name", "desc")


def test_create_clickup_ticket_falls_back_to_configured_list(fake):
    client = fake(payload={"id": "CU-1"})

    create_clickup_ticket("name", "desc")

    assert client.calls[0][1][0] == "default-list"


def test_create_clickup_ticket_errors_without_any_list(fake, monkeypatch):
    fake(payload={"id": "CU-1"})
    monkeypatch.delenv("CLICKUP_LIST_ID", raising=False)

    with pytest.raises(ClickUpTicketError, match="No ClickUp list"):
        create_clickup_ticket("name", "desc")


def test_create_clickup_ticket_passes_assignees_as_ints(fake):
    client = fake(payload={"id": "CU-1"})

    create_clickup_ticket("name", "desc", assignees=[123, "456"])

    assert client.calls[0][2]["assignees"] == [123, 456]


def test_create_clickup_ticket_omits_assignees_when_not_given(fake):
    client = fake(payload={"id": "CU-1"})

    create_clickup_ticket("name", "desc")

    assert "assignees" not in client.calls[0][2]


def test_create_clickup_ticket_passes_due_date(fake):
    client = fake(payload={"id": "CU-1"})

    create_clickup_ticket("name", "desc", due_date_ms=1735689600000)

    assert client.calls[0][2]["due_date"] == 1735689600000


def test_create_clickup_ticket_passes_parent(fake):
    client = fake(payload={"id": "CU-1"})

    create_clickup_ticket("name", "desc", parent="CU-parent")

    assert client.calls[0][2]["parent"] == "CU-parent"


def test_create_clickup_ticket_maps_priority_to_clickup_integer(fake):
    client = fake(payload={"id": "CU-1"})

    create_clickup_ticket("name", "desc", priority="urgent")

    assert client.calls[0][2]["priority"] == 1


def test_create_clickup_ticket_omits_priority_when_not_given(fake):
    client = fake(payload={"id": "CU-1"})

    create_clickup_ticket("name", "desc")

    assert "priority" not in client.calls[0][2]


def test_create_clickup_ticket_rejects_invalid_priority():
    with pytest.raises(ValueError, match="priority must be one of"):
        create_clickup_ticket("name", "desc", priority="urgentish")


def test_create_clickup_ticket_raises_on_api_error(fake):
    fake(raises=ClickUpAPIError(400, {"err": "boom"}))

    with pytest.raises(ClickUpTicketError, match="boom"):
        create_clickup_ticket("name", "desc")


def test_create_clickup_ticket_raises_when_id_missing(fake):
    fake(payload={"name": "no id here"})

    with pytest.raises(ClickUpTicketError, match="missing 'id'"):
        create_clickup_ticket("name", "desc")


def test_update_clickup_task_status_returns_payload(fake):
    client = fake(payload={"id": "CU-1", "status": {"status": "done"}})

    payload = update_clickup_task_status("CU-1", "done")

    assert payload["status"]["status"] == "done"
    assert client.calls[0] == ("update_task_status", ("CU-1", "done"), {})


def test_update_clickup_task_status_raises_on_api_error(fake):
    fake(raises=ClickUpAPIError(400, {"err": "Status not found"}))

    with pytest.raises(ClickUpTicketError, match="Status not found"):
        update_clickup_task_status("CU-1", "not-a-real-status")


def test_update_clickup_task_sends_only_given_fields(fake):
    client = fake(payload={"id": "CU-1"})

    update_clickup_task("CU-1", name="new name")

    assert client.calls[0][2] == {"name": "new name", "description": None}


def test_update_clickup_task_requires_a_field():
    with pytest.raises(ValueError, match="Provide a name and/or a description"):
        update_clickup_task("CU-1")


def test_list_clickup_teams_returns_payload(fake):
    client = fake(payload=[{"id": "T1", "name": "Team 1"}])

    teams = list_clickup_teams()

    assert teams == [{"id": "T1", "name": "Team 1"}]
    assert client.calls[0][0] == "get_teams"


def test_list_clickup_spaces_passes_team_id(fake):
    client = fake(payload=[{"id": "S1", "name": "Space 1"}])

    spaces = list_clickup_spaces("T1")

    assert spaces == [{"id": "S1", "name": "Space 1"}]
    assert client.calls[0] == ("get_spaces", ("T1",), {})


def test_list_clickup_folders_passes_space_id(fake):
    client = fake(payload=[{"id": "F1", "name": "Project-1"}])

    folders = list_clickup_folders("S1")

    assert folders == [{"id": "F1", "name": "Project-1"}]
    assert client.calls[0] == ("get_folders", ("S1",), {})


def test_list_clickup_lists_by_space_id(fake):
    client = fake(payload=[{"id": "L1", "name": "List 1"}])

    lists = list_clickup_lists("S1")

    assert lists == [{"id": "L1", "name": "List 1"}]
    assert client.calls[0][2] == {"space_id": "S1", "folder_id": None}


def test_list_clickup_lists_by_folder_id(fake):
    client = fake(payload=[{"id": "L2", "name": "Sprint backlog"}])

    lists = list_clickup_lists(folder_id="F1")

    assert lists == [{"id": "L2", "name": "Sprint backlog"}]
    assert client.calls[0][2] == {"space_id": None, "folder_id": "F1"}


def test_list_clickup_lists_requires_space_or_folder():
    with pytest.raises(ValueError, match="Provide either space_id or folder_id"):
        list_clickup_lists()


def test_list_clickup_tasks_passes_list_id(fake):
    client = fake(payload=[{"id": "T1", "name": "Task 1"}])

    tasks = list_clickup_tasks("L1")

    assert tasks == [{"id": "T1", "name": "Task 1"}]
    assert client.calls[0] == ("get_tasks", ("L1",), {})


def test_get_clickup_task_returns_payload(fake):
    client = fake(payload={"id": "T1", "name": "Task 1", "text_content": "desc"})

    task = get_clickup_task("T1")

    assert task == {"id": "T1", "name": "Task 1", "text_content": "desc"}
    assert client.calls[0] == ("get_task", ("T1",), {})


def test_get_clickup_task_raises_when_payload_is_not_an_object(fake):
    fake(payload=["not", "an", "object"])

    with pytest.raises(ClickUpReadError, match="Expected a JSON object"):
        get_clickup_task("T1")


def test_clickup_read_raises_on_api_error(fake):
    fake(raises=ClickUpAPIError(500, {"err": "upstream boom"}))

    with pytest.raises(ClickUpReadError, match="upstream boom"):
        list_clickup_teams()


def test_clickup_read_raises_on_network_failure(fake):
    fake(raises=requests.ConnectionError("no route to host"))

    with pytest.raises(ClickUpReadError, match="no route to host"):
        list_clickup_teams()


def test_clickup_read_raises_when_payload_is_not_a_list(fake):
    fake(payload={"not": "a list"})

    with pytest.raises(ClickUpReadError, match="Expected a JSON array"):
        list_clickup_teams()


def test_clickup_read_raises_without_credentials(monkeypatch):
    monkeypatch.delenv("CLICKUP_API_TOKEN", raising=False)

    with pytest.raises(ClickUpReadError, match="CLICKUP_API_TOKEN is not set"):
        list_clickup_teams()
