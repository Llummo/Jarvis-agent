from fastapi.testclient import TestClient

from meta_harness.linear_bridge import LinearIssueError, LinearReadError
from meta_harness.webapp.app import app

client = TestClient(app)


def test_get_teams(monkeypatch):
    monkeypatch.setattr(
        "meta_harness.webapp.routes_linear.list_linear_teams",
        lambda **kw: [{"id": "t1", "name": "Sigo", "key": "SIG"}],
    )

    response = client.get("/api/linear/teams")

    assert response.status_code == 200
    assert response.json()[0]["key"] == "SIG"


def test_get_states(monkeypatch):
    monkeypatch.setattr(
        "meta_harness.webapp.routes_linear.list_linear_states",
        lambda team_id, **kw: [{"id": "s1", "name": "Todo", "type": "unstarted"}],
    )

    response = client.get("/api/linear/states", params={"team_id": "t1"})

    assert response.status_code == 200
    assert response.json()[0]["name"] == "Todo"


def test_get_members(monkeypatch):
    monkeypatch.setattr(
        "meta_harness.webapp.routes_linear.list_linear_members",
        lambda team_id, **kw: [{"id": "u1", "name": "Ana", "email": "a@x.com"}],
    )

    response = client.get("/api/linear/members", params={"team_id": "t1"})

    assert response.status_code == 200
    assert response.json()[0]["email"] == "a@x.com"


def test_get_issues(monkeypatch):
    monkeypatch.setattr(
        "meta_harness.webapp.routes_linear.list_linear_issues",
        lambda team_id, **kw: [{"id": "i1", "identifier": "SIG-1", "title": "Fix"}],
    )

    response = client.get("/api/linear/issues", params={"team_id": "t1"})

    assert response.status_code == 200
    assert response.json()[0]["identifier"] == "SIG-1"


def test_get_issues_read_error_returns_502(monkeypatch):
    def boom(team_id, **kw):
        raise LinearReadError("linear is down")

    monkeypatch.setattr("meta_harness.webapp.routes_linear.list_linear_issues", boom)

    response = client.get("/api/linear/issues", params={"team_id": "t1"})

    assert response.status_code == 502
    assert "linear is down" in response.json()["detail"]


def test_create_issues_partial_failure_reports_both_results(monkeypatch):
    def fake_create(team_id, title, description, **kw):
        if title == "Ticket A":
            return "i1"
        raise LinearIssueError("Linear rejected it")

    monkeypatch.setattr("meta_harness.webapp.routes_linear.create_linear_issue", fake_create)

    response = client.post(
        "/api/linear/issues",
        json={
            "team_id": "t1",
            "tickets": [
                {"title": "Ticket A", "description": "d1", "acceptance_criteria": [], "priority": "normal"},
                {"title": "Ticket B", "description": "d2", "acceptance_criteria": [], "priority": "normal"},
            ],
        },
    )

    assert response.status_code == 200
    results = response.json()["results"]
    assert results[0]["ok"] is True
    assert results[0]["linear_issue_id"] == "i1"
    assert results[1]["ok"] is False
    assert "Linear rejected it" in results[1]["error"]


def test_create_issues_passes_assignee_and_due_date(monkeypatch):
    captured = {}

    def fake_create(team_id, title, description, **kw):
        captured.update(kw)
        return "i1"

    monkeypatch.setattr("meta_harness.webapp.routes_linear.create_linear_issue", fake_create)

    client.post(
        "/api/linear/issues",
        json={
            "team_id": "t1",
            "tickets": [
                {
                    "title": "Ticket A", "description": "d", "acceptance_criteria": [], "priority": "urgent",
                    "assignee_linear_id": "uuid-123", "due_date": "2026-08-24",
                },
            ],
        },
    )

    assert captured["priority"] == "urgent"
    assert captured["due_date"] == "2026-08-24"


def test_create_issues_description_includes_user_story_and_criteria(monkeypatch):
    captured = {}

    def fake_create(team_id, title, description, **kw):
        captured["description"] = description
        return "i1"

    monkeypatch.setattr("meta_harness.webapp.routes_linear.create_linear_issue", fake_create)

    client.post(
        "/api/linear/issues",
        json={
            "team_id": "t1",
            "tickets": [
                {
                    "title": "Ticket A",
                    "user_story": "As a user, I want to log in, so I can access my account.",
                    "description": "Do the thing.",
                    "acceptance_criteria": ["Given x, when y, then z."],
                    "priority": "normal",
                },
            ],
        },
    )

    assert "As a user, I want to log in" in captured["description"]
    assert "Do the thing." in captured["description"]
    assert "Given x, when y, then z." in captured["description"]


def test_set_issue_state(monkeypatch):
    monkeypatch.setattr(
        "meta_harness.webapp.routes_linear.update_linear_issue_state",
        lambda issue_id, state_id, **kw: {"id": issue_id, "state": {"id": state_id, "name": "Done"}},
    )

    response = client.post("/api/linear/issues/i1/state", json={"state_id": "s2"})

    assert response.status_code == 200
    assert response.json()["state"]["name"] == "Done"


def test_set_issue_state_error_returns_502(monkeypatch):
    def boom(issue_id, state_id, **kw):
        raise LinearIssueError("bad state id")

    monkeypatch.setattr("meta_harness.webapp.routes_linear.update_linear_issue_state", boom)

    response = client.post("/api/linear/issues/i1/state", json={"state_id": "nope"})

    assert response.status_code == 502
