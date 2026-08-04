"""The two-step API: writing a plan reads only, running it writes."""

import pytest
from fastapi.testclient import TestClient

from meta_harness.webapp import routes_qa_tests
from meta_harness.webapp.app import create_app

HOUSE = """📍 Ruta / Vista UI: /erp/talent/people

🔌 Endpoint Backend: POST /api/v1/talent/people

✅ CRITERIOS DE ACEPTACIÓN

📌 Criterio 1: Alta correcta

* Dado que el formulario es válido,
* cuando se confirme,
* entonces la persona queda registrada.
"""

PLAN = {
    "ticket_id": "SIG-109",
    "route": "/erp/talent/people",
    "cases": [
        {
            "name": "01-alta",
            "criterion": "entonces la persona queda registrada",
            "steps": [{"action": "goto", "target": "/erp/talent/people"}],
            "expectation": {"ui_text": "Persona registrada", "api_path": "/api/v1/talent/people",
                            "api_method": "POST", "api_status_range": "2xx"},
        }
    ],
}


@pytest.fixture
def client():
    return TestClient(create_app())


def test_environments_reports_what_is_ready(client):
    body = client.get("/api/qa/tests/environments").json()
    names = [e["name"] for e in body["environments"]]
    assert names == ["local", "qa"]
    assert "prod" not in names


def test_plan_is_written_from_the_ticket(client, monkeypatch):
    monkeypatch.setattr(routes_qa_tests, "_fetch_ticket", lambda t, tr: ("Alta", HOUSE))
    monkeypatch.setattr(
        routes_qa_tests, "author_plan",
        lambda *a, **k: __import__("meta_harness.qa.plan", fromlist=["TestPlan"]).TestPlan.from_dict(PLAN),
    )
    body = client.post("/api/qa/tests/plan", json={"ticket_id": "SIG-109"}).json()
    assert body["cases"][0]["expectation"]["api_path"] == "/api/v1/talent/people"


def test_an_untestable_ticket_is_422_not_502(client, monkeypatch):
    """A ticket with no route is not an outage — it is a ticket that cannot be
    tested this way, and the message has to say so."""
    monkeypatch.setattr(routes_qa_tests, "_fetch_ticket", lambda t, tr: ("t", "sin formato"))
    response = client.post("/api/qa/tests/plan", json={"ticket_id": "SIG-1"})
    assert response.status_code == 422
    assert "ruta" in response.json()["detail"]


def test_running_a_malformed_plan_is_400(client):
    response = client.post("/api/qa/tests/run", json={"plan": {"ticket_id": "x", "cases": []}})
    assert response.status_code == 400


def test_a_production_url_is_refused_at_the_api(client):
    """The guard has to hold at the edge, not only in the library."""
    response = client.post(
        "/api/qa/tests/run",
        json={"plan": PLAN, "environment": "local", "ui_url": "https://sigo.metrica-global.com"},
    )
    assert response.status_code == 400
    assert "production" in response.json()["detail"]


def test_run_returns_the_report(client, monkeypatch):
    from pathlib import Path

    from meta_harness.qa.runner import CaseResult, RunResult

    def fake_run(plan, environment, **kwargs):
        run = RunResult(
            ticket_id=plan.ticket_id, ticket_title="", environment=environment.describe(),
            route=plan.route, started_at="2026-08-04T00:00:00Z",
            evidence_dir=Path(kwargs["evidence_root"]) / "x",
        )
        run.evidence_dir.mkdir(parents=True, exist_ok=True)
        run.cases.append(CaseResult(name="01-alta", criterion="c", result="passed",
                                    ui_ok=True, api_ok=True))
        return run

    monkeypatch.setattr(routes_qa_tests, "run_plan", fake_run)
    body = client.post("/api/qa/tests/run", json={"plan": PLAN, "environment": "local"}).json()
    assert body["passed_count"] == 1
    assert "report_markdown" in body
    assert "**UI:**" in body["report_markdown"]
