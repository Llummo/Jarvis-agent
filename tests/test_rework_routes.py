import pytest
from fastapi.testclient import TestClient

from meta_harness.webapp import routes_rework
from meta_harness.webapp.app import create_app
from meta_harness.rework.models import (
    ParentOption,
    ReworkOutcome,
    ReworkPreview,
    ReworkReport,
    WorkflowState,
)


@pytest.fixture
def client():
    return TestClient(create_app())


def _preview():
    return ReworkPreview(
        team_id="team-1",
        resolutions=[],
        cancelled_state=WorkflowState(id="sc", name="Cancelled", type="canceled"),
        warnings=["one warning"],
    )


def test_preview_returns_the_resolution_payload(client, monkeypatch):
    monkeypatch.setattr(routes_rework, "preview_rework", lambda *a, **k: _preview())
    response = client.post("/api/rework/preview", json={"team_id": "team-1", "names": "6.5 Tabla"})
    assert response.status_code == 200
    body = response.json()
    assert body["cancelled_state"]["id"] == "sc"
    assert body["warnings"] == ["one warning"]


def test_preview_does_not_reason_unless_asked(client, monkeypatch):
    seen = {}

    def fake(team_id, names, **kwargs):
        seen["reasoner"] = kwargs.get("reasoner")
        return _preview()

    monkeypatch.setattr(routes_rework, "preview_rework", fake)
    client.post("/api/rework/preview", json={"team_id": "t", "names": "x"})
    assert seen["reasoner"] is None

    client.post("/api/rework/preview", json={"team_id": "t", "names": "x", "use_reasoning": True})
    assert seen["reasoner"] is not None


def test_preview_surfaces_an_upstream_failure_as_502(client, monkeypatch):
    def boom(*args, **kwargs):
        raise routes_rework.LinearReadError("Linear is down")

    monkeypatch.setattr(routes_rework, "preview_rework", boom)
    response = client.post("/api/rework/preview", json={"team_id": "t", "names": "x"})
    assert response.status_code == 502
    assert "Linear is down" in response.json()["detail"]


def test_parents_endpoint_returns_candidates(client, monkeypatch):
    monkeypatch.setattr(
        routes_rework,
        "find_parent_candidates",
        lambda team, q, **k: [ParentOption("i1", "SIG-9", "Módulo de selección", "Todo")],
    )
    response = client.get("/api/rework/parents", params={"team_id": "t", "q": "modulo"})
    assert response.status_code == 200
    assert response.json()[0]["identifier"] == "SIG-9"


def test_parents_endpoint_requires_a_query(client):
    assert client.get("/api/rework/parents", params={"team_id": "t", "q": ""}).status_code == 422


def test_apply_executes_the_plan(client, monkeypatch):
    captured = {}

    def fake(plan, **kwargs):
        captured["plan"] = plan
        return ReworkReport(
            parent_issue_id=plan.parent_issue_id,
            outcomes=[ReworkOutcome("i1", "SIG-1", "old", created_issue_id="n1", cancelled=True)],
        )

    monkeypatch.setattr(routes_rework, "execute_rework_plan", fake)
    response = client.post(
        "/api/rework/apply",
        json={
            "team_id": "t",
            "parent_issue_id": "p1",
            "items": [{"issue_id": "i1", "identifier": "SIG-1", "original_title": "old"}],
            "cancelled_state_id": "sc",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["created_count"] == 1
    assert captured["plan"].parent_issue_id == "p1"


def test_apply_rejects_an_invalid_plan_as_400(client, monkeypatch):
    """A plan mistake is the caller's, and must not look like an outage."""
    called = []
    monkeypatch.setattr(routes_rework, "execute_rework_plan", lambda *a, **k: called.append(1))
    response = client.post(
        "/api/rework/apply",
        json={"team_id": "t", "parent_issue_id": "", "items": [{"issue_id": "i1"}],
              "cancelled_state_id": "sc"},
    )
    assert response.status_code == 400
    assert "parent" in response.json()["detail"]
    assert called == []


def test_apply_rejects_reworking_the_parent_itself(client, monkeypatch):
    monkeypatch.setattr(routes_rework, "execute_rework_plan", lambda *a, **k: None)
    response = client.post(
        "/api/rework/apply",
        json={"team_id": "t", "parent_issue_id": "p1", "items": [{"issue_id": "p1"}],
              "cancelled_state_id": "sc"},
    )
    assert response.status_code == 400
    assert "cannot also be" in response.json()["detail"]


def test_apply_requires_a_cancelled_state_when_cancelling(client, monkeypatch):
    monkeypatch.setattr(routes_rework, "execute_rework_plan", lambda *a, **k: None)
    response = client.post(
        "/api/rework/apply",
        json={"team_id": "t", "parent_issue_id": "p1", "items": [{"issue_id": "i1"}]},
    )
    assert response.status_code == 400
    assert "cancelled state" in response.json()["detail"]


def test_apply_reports_stranded_originals(client, monkeypatch):
    monkeypatch.setattr(
        routes_rework,
        "execute_rework_plan",
        lambda plan, **k: ReworkReport(
            parent_issue_id=plan.parent_issue_id,
            outcomes=[ReworkOutcome("i1", "SIG-1", "old", created_issue_id="n1", cancelled=False)],
        ),
    )
    response = client.post(
        "/api/rework/apply",
        json={"team_id": "t", "parent_issue_id": "p1", "items": [{"issue_id": "i1"}],
              "cancelled_state_id": "sc"},
    )
    body = response.json()
    assert body["stranded_count"] == 1
    assert body["outcomes"][0]["needs_attention"] is True


def test_static_files_must_be_revalidated(client):
    """A cached app.js shows the previous UI and reads as a broken feature."""
    response = client.get("/app.js")
    assert response.status_code == 200
    assert "no-cache" in response.headers.get("cache-control", "")
