from fastapi.testclient import TestClient

from meta_harness.clickup_bridge import ClickUpReadError
from meta_harness.webapp.app import app

client = TestClient(app)


def test_get_teams_returns_canned_data(monkeypatch):
    monkeypatch.setattr(
        "meta_harness.webapp.routes_clickup.list_clickup_teams",
        lambda **kw: [{"id": "T1", "name": "Team 1"}],
    )

    response = client.get("/api/clickup/teams")

    assert response.status_code == 200
    assert response.json() == [{"id": "T1", "name": "Team 1"}]


def test_get_spaces_passes_team_id(monkeypatch):
    captured = {}

    def fake_list_spaces(team_id, **kw):
        captured["team_id"] = team_id
        return [{"id": "S1", "name": "Space 1"}]

    monkeypatch.setattr("meta_harness.webapp.routes_clickup.list_clickup_spaces", fake_list_spaces)

    response = client.get("/api/clickup/spaces", params={"team_id": "T1"})

    assert response.status_code == 200
    assert captured["team_id"] == "T1"
    assert response.json() == [{"id": "S1", "name": "Space 1"}]


def test_get_lists_passes_space_id(monkeypatch):
    captured = {}

    def fake_list_lists(space_id, **kw):
        captured["space_id"] = space_id
        return [{"id": "L1", "name": "List 1"}]

    monkeypatch.setattr("meta_harness.webapp.routes_clickup.list_clickup_lists", fake_list_lists)

    response = client.get("/api/clickup/lists", params={"space_id": "S1"})

    assert response.status_code == 200
    assert captured["space_id"] == "S1"


def test_get_lists_passes_folder_id(monkeypatch):
    captured = {}

    def fake_list_lists(space_id, folder_id=None, **kw):
        captured["space_id"] = space_id
        captured["folder_id"] = folder_id
        return [{"id": "L2", "name": "Sprint backlog"}]

    monkeypatch.setattr("meta_harness.webapp.routes_clickup.list_clickup_lists", fake_list_lists)

    response = client.get("/api/clickup/lists", params={"folder_id": "F1"})

    assert response.status_code == 200
    assert captured["space_id"] is None
    assert captured["folder_id"] == "F1"


def test_get_lists_without_space_or_folder_returns_400():
    response = client.get("/api/clickup/lists")

    assert response.status_code == 400


def test_get_folders_passes_space_id(monkeypatch):
    captured = {}

    def fake_list_folders(space_id, **kw):
        captured["space_id"] = space_id
        return [{"id": "F1", "name": "Project-1"}]

    monkeypatch.setattr("meta_harness.webapp.routes_clickup.list_clickup_folders", fake_list_folders)

    response = client.get("/api/clickup/folders", params={"space_id": "S1"})

    assert response.status_code == 200
    assert captured["space_id"] == "S1"
    assert response.json() == [{"id": "F1", "name": "Project-1"}]


def test_get_tasks_passes_list_id(monkeypatch):
    captured = {}

    def fake_list_tasks(list_id, **kw):
        captured["list_id"] = list_id
        return [{"id": "T1", "name": "Task 1"}]

    monkeypatch.setattr("meta_harness.webapp.routes_clickup.list_clickup_tasks", fake_list_tasks)

    response = client.get("/api/clickup/tasks", params={"list_id": "L1"})

    assert response.status_code == 200
    assert captured["list_id"] == "L1"
    assert response.json() == [{"id": "T1", "name": "Task 1"}]


def test_clickup_read_error_returns_502(monkeypatch):
    def boom(**kw):
        raise ClickUpReadError("p-harness CLI unavailable")

    monkeypatch.setattr("meta_harness.webapp.routes_clickup.list_clickup_teams", boom)

    response = client.get("/api/clickup/teams")

    assert response.status_code == 502
    assert "p-harness CLI unavailable" in response.json()["detail"]


def test_static_assets_must_be_revalidated():
    """StaticFiles sends ETag but no Cache-Control, leaving browsers free to
    heuristically cache. Here that produces a page whose JS and markup are from
    different revisions — controls that silently do nothing, with no error
    anywhere to explain it."""
    from fastapi.testclient import TestClient

    from meta_harness.webapp.app import create_app

    with TestClient(create_app()) as client:
        response = client.get("/app.js")

    assert response.status_code == 200
    assert "no-cache" in response.headers.get("cache-control", "")
    # Revalidation, not re-download: an unchanged asset must still 304.
    assert response.headers.get("etag")
