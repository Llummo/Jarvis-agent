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
                    "user_story": "Como usuario,\nquiero iniciar sesión,\npara acceder a mi cuenta.",
                    "description": "Do the thing.",
                    "acceptance_criteria": [
                        {"name": "Inicio de sesión", "given": "x", "when": "y", "then": "z"}
                    ],
                    "priority": "normal",
                },
            ],
        },
    )

    assert "quiero iniciar sesión" in captured["description"]
    assert "Do the thing." in captured["description"]
    assert "📌 Criterio 1: Inicio de sesión" in captured["description"]
    assert "* Dado que x," in captured["description"]
    assert "* cuando y," in captured["description"]
    assert "* entonces z." in captured["description"]


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


def test_create_issues_nests_subtickets_under_their_parent(monkeypatch):
    calls = []

    def fake_create(team_id, title, description, **kw):
        calls.append((title, kw.get("parent_id")))
        return {"Parent feature": "parent-1"}.get(title, f"child-{len(calls)}")

    monkeypatch.setattr("meta_harness.webapp.routes_linear.create_linear_issue", fake_create)

    response = client.post(
        "/api/linear/issues",
        json={
            "team_id": "t1",
            "tickets": [
                {"title": "Child A", "description": "d", "acceptance_criteria": [],
                 "priority": "normal", "parent_title": "Parent feature"},
                {"title": "Parent feature", "description": "d", "acceptance_criteria": [], "priority": "normal"},
            ],
        },
    )

    assert response.status_code == 200
    # The parent is created first even though it came second in the payload...
    assert calls[0] == ("Parent feature", None)
    # ...so the child can be nested under its real id.
    assert calls[1] == ("Child A", "parent-1")
    # Results still come back in the caller's original order.
    assert [r["ticket"]["title"] for r in response.json()["results"]] == ["Child A", "Parent feature"]


def test_create_issues_orphan_parent_reference_still_creates_the_ticket(monkeypatch):
    calls = []

    def fake_create(team_id, title, description, **kw):
        calls.append((title, kw.get("parent_id")))
        return "i1"

    monkeypatch.setattr("meta_harness.webapp.routes_linear.create_linear_issue", fake_create)

    response = client.post(
        "/api/linear/issues",
        json={
            "team_id": "t1",
            "tickets": [
                {"title": "Orphan", "description": "d", "acceptance_criteria": [],
                 "priority": "normal", "parent_title": "Does not exist"},
            ],
        },
    )

    assert response.status_code == 200
    assert response.json()["results"][0]["ok"] is True
    assert calls[0] == ("Orphan", None)  # nested nowhere rather than failing


def test_linear_description_follows_the_house_format_exactly(monkeypatch):
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
                    "title": "Registro Maestro de Personas",
                    "epic": "GESTIÓN DE POOL DE TALENTO (PERSONAS)",
                    "ui_route": "/erp/talent/people",
                    "backend_endpoint": "GET / POST /api/v1/talent/people",
                    "user_story": "Como reclutador,\nquiero registrar personas,\npara centralizar el pool.",
                    "description": "Registro transversal de personas.",
                    "technical_notes": "Límite de Importación: 100 por lote.\nMatriz de Permisos: talent:read, talent:write",
                    "acceptance_criteria": [
                        {"name": "Carga de CV", "given": "el usuario está en la pantalla",
                         "when": "adjunte un CV en PDF", "then": "el sistema extrae los datos"},
                    ],
                    "priority": "high",
                },
            ],
        },
    )

    body = captured["description"]
    assert body.startswith("📄 USER STORY: GESTIÓN DE POOL DE TALENTO (PERSONAS)")
    assert "Título: Registro Maestro de Personas" in body
    assert "📍 Ruta / Vista UI: /erp/talent/people" in body
    assert "🔌 Endpoint Backend: GET / POST /api/v1/talent/people" in body
    assert "📝 DESCRIPCIÓN" in body
    # Como / quiero / para each on their own line
    assert "Como reclutador," in body
    assert "quiero registrar personas," in body
    assert "para centralizar el pool." in body
    assert "🖼️ RECURSOS VISUALES Y REFERENCIAS (OPCIONAL)" in body
    # No images on this ticket, so the section states that plainly rather
    # than showing empty template brackets.
    assert "- No se encontraron referencias visuales." in body
    assert "[Link a Figma]" not in body
    assert "✅ CRITERIOS DE ACEPTACIÓN" in body
    assert "📌 Criterio 1: Carga de CV" in body
    assert "* Dado que el usuario está en la pantalla," in body
    assert "* cuando adjunte un CV en PDF," in body
    assert "* entonces el sistema extrae los datos." in body
    # the trailing blank-criterion template
    assert "📌 Criterio X: [Espacio para criterios adicionales]" in body
    assert "* Dado que [condición inicial]," in body
    assert "* cuando [acción o evento]," in body
    assert "* entonces [resultado esperado]." in body
    assert "🛠️ NOTAS TÉCNICAS Y ADICIONALES (opcional)" in body
    assert "* Límite de Importación: 100 por lote." in body
    assert "* Matriz de Permisos: talent:read, talent:write" in body
