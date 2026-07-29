import pytest
import responses

from meta_harness.trackers import ClickUpClient
from meta_harness.trackers.clickup import API_BASE, ClickUpAPIError


@responses.activate
def test_get_teams():
    responses.add(
        responses.GET,
        "https://api.clickup.com/api/v2/team",
        json={"teams": [{"id": "123", "name": "My Team"}]},
        status=200,
    )
    client = ClickUpClient(api_token="fake-token")

    teams = client.get_teams()

    assert teams == [{"id": "123", "name": "My Team"}]


@responses.activate
def test_get_task():
    responses.add(
        responses.GET,
        "https://api.clickup.com/api/v2/task/task1",
        json={"id": "task1", "name": "Do the thing"},
        status=200,
    )
    client = ClickUpClient(api_token="fake-token")

    task = client.get_task("task1")

    assert task == {"id": "task1", "name": "Do the thing"}


@responses.activate
def test_get_folders():
    responses.add(
        responses.GET,
        "https://api.clickup.com/api/v2/space/space123/folder",
        json={"folders": [{"id": "folder1", "name": "Project-1"}]},
        status=200,
    )
    client = ClickUpClient(api_token="fake-token")

    folders = client.get_folders("space123")

    assert folders == [{"id": "folder1", "name": "Project-1"}]


@responses.activate
def test_get_tasks():
    responses.add(
        responses.GET,
        "https://api.clickup.com/api/v2/list/list123/task",
        json={"tasks": [{"id": "task1", "name": "Do the thing"}]},
        status=200,
    )
    client = ClickUpClient(api_token="fake-token")

    tasks = client.get_tasks("list123")

    assert tasks == [{"id": "task1", "name": "Do the thing"}]


@responses.activate
def test_create_task():
    responses.add(
        responses.POST,
        "https://api.clickup.com/api/v2/list/list123/task",
        json={"id": "task1", "name": "Do the thing"},
        status=200,
    )
    client = ClickUpClient(api_token="fake-token")

    task = client.create_task("list123", "Do the thing")

    assert task["name"] == "Do the thing"


@responses.activate
def test_update_task_status():
    responses.add(
        responses.PUT,
        "https://api.clickup.com/api/v2/task/task1",
        json={"id": "task1", "status": {"status": "done"}},
        status=200,
    )
    client = ClickUpClient(api_token="fake-token")

    task = client.update_task_status("task1", "done")

    assert task["status"]["status"] == "done"
    assert responses.calls[0].request.body == b'{"status": "done"}'


@responses.activate
def test_update_task_status_raises_on_invalid_status():
    responses.add(
        responses.PUT,
        "https://api.clickup.com/api/v2/task/task1",
        json={"err": "Status not found", "ECODE": "STATUS_002"},
        status=400,
    )
    client = ClickUpClient(api_token="fake-token")

    with pytest.raises(ClickUpAPIError):
        client.update_task_status("task1", "not-a-real-status")


# ---------------------------------------------------------------------------
# Pagination — ClickUp returns at most 100 tasks per page, so a longer list
# was silently truncated before this.
# ---------------------------------------------------------------------------


@responses.activate
def test_get_tasks_follows_every_page():
    responses.add(
        responses.GET, f"{API_BASE}/list/L1/task",
        json={"tasks": [{"id": f"t{n}"} for n in range(100)], "last_page": False}, status=200,
    )
    responses.add(
        responses.GET, f"{API_BASE}/list/L1/task",
        json={"tasks": [{"id": "t100"}], "last_page": True}, status=200,
    )

    tasks = ClickUpClient("token").get_tasks("L1")

    assert len(tasks) == 101
    assert "page=0" in responses.calls[0].request.url
    assert "page=1" in responses.calls[1].request.url


@responses.activate
def test_get_tasks_stops_at_the_limit():
    responses.add(
        responses.GET, f"{API_BASE}/list/L1/task",
        json={"tasks": [{"id": f"t{n}"} for n in range(100)], "last_page": False}, status=200,
    )

    tasks = ClickUpClient("token").get_tasks("L1", limit=5)

    assert len(tasks) == 5
    assert len(responses.calls) == 1


@responses.activate
def test_get_tasks_single_page_makes_one_request():
    responses.add(
        responses.GET, f"{API_BASE}/list/L1/task",
        json={"tasks": [{"id": "t1"}], "last_page": True}, status=200,
    )

    assert len(ClickUpClient("token").get_tasks("L1")) == 1
    assert len(responses.calls) == 1


@responses.activate
def test_get_tasks_asks_clickup_to_include_subtasks():
    # Without this, tickets created as subtasks (ticket hierarchies) are
    # silently omitted and a full list looks like most of it went missing.
    responses.add(
        responses.GET, f"{API_BASE}/list/L1/task",
        json={"tasks": [{"id": "t1"}], "last_page": True}, status=200,
    )

    ClickUpClient("token").get_tasks("L1")

    assert "subtasks=true" in responses.calls[0].request.url


@responses.activate
def test_non_json_error_body_keeps_the_status_code():
    """A gateway/HTML error must not be masked by a decode error from the
    error handler itself."""
    responses.add(
        responses.GET,
        "https://api.clickup.com/api/v2/team",
        body="<html>502 Bad Gateway</html>",
        status=502,
        content_type="text/html",
    )
    client = ClickUpClient(api_token="fake-token")

    with pytest.raises(ClickUpAPIError) as excinfo:
        client.get_teams()

    assert excinfo.value.status_code == 502
    assert "502 Bad Gateway" in excinfo.value.payload["error"]
