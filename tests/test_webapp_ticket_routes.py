from fastapi.testclient import TestClient

from meta_harness.clickup_bridge import ClickUpTicketError
from meta_harness.ticket_generator import (
    ClaudeNotFoundError,
    ProposedTicket,
    TicketExtractionError,
    TicketParseError,
)
from meta_harness.webapp.app import app

client = TestClient(app)


def test_generate_tickets_returns_proposed_tickets(monkeypatch):
    monkeypatch.setattr(
        "meta_harness.webapp.routes_tickets.generate_tickets_from_file",
        lambda filename, content, **kw: (
            [
                ProposedTicket(
                    title="Build login page",
                    user_story="As a user, I want to log in, so I can access my account.",
                    description="desc", acceptance_criteria=["Given ..., when ..., then ..."],
                    priority="high", category="backend",
                )
            ],
            ["some warning"],
        ),
    )

    response = client.post("/api/tickets/generate", files={"file": ("spec.txt", b"some text", "text/plain")})

    assert response.status_code == 200
    body = response.json()
    assert len(body["tickets"]) == 1
    ticket = body["tickets"][0]
    assert ticket["title"] == "TAB-01 | Build login page"
    assert ticket["user_story"] == "As a user, I want to log in, so I can access my account."
    assert ticket["description"] == "desc"
    assert ticket["acceptance_criteria"] == ["Given ..., when ..., then ..."]
    assert ticket["priority"] == "high"
    assert ticket["category"] == "backend"
    assert ticket["due_date"] is not None  # apply_sprint_due_dates always sets this
    assert body["warnings"] == ["some warning"]


def test_generate_tickets_numbers_by_category_and_respects_start_numbers(monkeypatch):
    monkeypatch.setattr(
        "meta_harness.webapp.routes_tickets.generate_tickets_from_file",
        lambda filename, content, **kw: (
            [
                ProposedTicket(title="General planning", description="d", acceptance_criteria=[], category="mundane"),
                ProposedTicket(title="Build API endpoint", description="d", acceptance_criteria=[], category="backend"),
                ProposedTicket(title="Build login page", description="d", acceptance_criteria=[], category="frontend"),
            ],
            [],
        ),
    )

    response = client.post(
        "/api/tickets/generate",
        files={"file": ("spec.txt", b"some text", "text/plain")},
        data={"start_mundane": "2"},
    )

    assert response.status_code == 200
    titles = [t["title"] for t in response.json()["tickets"]]
    assert titles == [
        "TAM-02 | General planning",
        "TAB-01 | Build API endpoint",
        "TAF-01 | Build login page",
    ]


def test_generate_tickets_numbering_defaults_to_one_when_no_start_given(monkeypatch):
    monkeypatch.setattr(
        "meta_harness.webapp.routes_tickets.generate_tickets_from_file",
        lambda filename, content, **kw: (
            [ProposedTicket(title="Set up CI pipeline", description="d", acceptance_criteria=[], category="deployment")],
            [],
        ),
    )

    response = client.post("/api/tickets/generate", files={"file": ("spec.txt", b"some text", "text/plain")})

    assert response.json()["tickets"][0]["title"] == "TAD-01 | Set up CI pipeline"


def test_generate_tickets_extraction_error_returns_400(monkeypatch):
    def boom(filename, content, **kw):
        raise TicketExtractionError("unsupported file type")

    monkeypatch.setattr("meta_harness.webapp.routes_tickets.generate_tickets_from_file", boom)

    response = client.post("/api/tickets/generate", files={"file": ("spec.docx", b"junk", "application/octet-stream")})

    assert response.status_code == 400
    assert "unsupported file type" in response.json()["detail"]


def test_generate_tickets_claude_not_found_returns_503(monkeypatch):
    def boom(filename, content, **kw):
        raise ClaudeNotFoundError("no claude binary")

    monkeypatch.setattr("meta_harness.webapp.routes_tickets.generate_tickets_from_file", boom)

    response = client.post("/api/tickets/generate", files={"file": ("spec.txt", b"text", "text/plain")})

    assert response.status_code == 503
    assert "no claude binary" in response.json()["detail"]


def test_generate_tickets_parse_error_returns_502(monkeypatch):
    def boom(filename, content, **kw):
        raise TicketParseError("no usable tickets")

    monkeypatch.setattr("meta_harness.webapp.routes_tickets.generate_tickets_from_file", boom)

    response = client.post("/api/tickets/generate", files={"file": ("spec.txt", b"text", "text/plain")})

    assert response.status_code == 502
    assert "no usable tickets" in response.json()["detail"]


def test_create_tickets_partial_failure_reports_both_results(monkeypatch):
    def fake_create(name, description, *, list_id=None, priority=None, project_path=None, **kwargs):
        if name == "Ticket A":
            return "CU-1"
        raise ClickUpTicketError("ClickUp API error 500")

    monkeypatch.setattr("meta_harness.webapp.routes_tickets.create_clickup_ticket", fake_create)

    response = client.post(
        "/api/tickets/create",
        json={
            "tickets": [
                {"title": "Ticket A", "description": "d1", "acceptance_criteria": [], "priority": "normal"},
                {"title": "Ticket B", "description": "d2", "acceptance_criteria": [], "priority": "normal"},
            ]
        },
    )

    assert response.status_code == 200
    results = response.json()["results"]
    assert len(results) == 2
    assert results[0]["ok"] is True
    assert results[0]["clickup_task_id"] == "CU-1"
    assert results[1]["ok"] is False
    assert "ClickUp API error 500" in results[1]["error"]


def test_create_tickets_passes_list_id_and_priority(monkeypatch):
    captured = {}

    def fake_create(name, description, *, list_id=None, priority=None, project_path=None, **kwargs):
        captured["list_id"] = list_id
        captured["priority"] = priority
        return "CU-1"

    monkeypatch.setattr("meta_harness.webapp.routes_tickets.create_clickup_ticket", fake_create)

    response = client.post(
        "/api/tickets/create",
        json={
            "tickets": [
                {"title": "Ticket A", "description": "d1", "acceptance_criteria": [], "priority": "urgent"},
            ],
            "list_id": "L123",
        },
    )

    assert response.status_code == 200
    assert captured["list_id"] == "L123"
    assert captured["priority"] == "urgent"


def test_create_tickets_description_includes_acceptance_criteria(monkeypatch):
    captured = {}

    def fake_create(name, description, *, list_id=None, priority=None, project_path=None, **kwargs):
        captured["description"] = description
        return "CU-1"

    monkeypatch.setattr("meta_harness.webapp.routes_tickets.create_clickup_ticket", fake_create)

    client.post(
        "/api/tickets/create",
        json={
            "tickets": [
                {
                    "title": "Ticket A",
                    "description": "Do the thing.",
                    "acceptance_criteria": ["Criterion one", "Criterion two"],
                    "priority": "normal",
                },
            ]
        },
    )

    assert "Do the thing." in captured["description"]
    assert "Criterion one" in captured["description"]
    assert "Criterion two" in captured["description"]


def test_create_tickets_description_includes_user_story(monkeypatch):
    captured = {}

    def fake_create(name, description, *, list_id=None, priority=None, project_path=None, **kwargs):
        captured["description"] = description
        return "CU-1"

    monkeypatch.setattr("meta_harness.webapp.routes_tickets.create_clickup_ticket", fake_create)

    client.post(
        "/api/tickets/create",
        json={
            "tickets": [
                {
                    "title": "Ticket A",
                    "user_story": "As a user, I want to log in, so I can access my account.",
                    "description": "Do the thing.",
                    "acceptance_criteria": [],
                    "priority": "normal",
                },
            ]
        },
    )

    assert "As a user, I want to log in, so I can access my account." in captured["description"]


def test_create_tickets_description_omits_user_story_line_when_absent(monkeypatch):
    captured = {}

    def fake_create(name, description, *, list_id=None, priority=None, project_path=None, **kwargs):
        captured["description"] = description
        return "CU-1"

    monkeypatch.setattr("meta_harness.webapp.routes_tickets.create_clickup_ticket", fake_create)

    client.post(
        "/api/tickets/create",
        json={
            "tickets": [
                {"title": "Ticket A", "description": "Do the thing.", "acceptance_criteria": [], "priority": "normal"},
            ]
        },
    )

    assert captured["description"].startswith("Do the thing.")


def test_create_tickets_passes_assignees_and_due_date(monkeypatch):
    captured = {}

    def fake_create(name, description, *, list_id=None, priority=None, project_path=None, **kwargs):
        captured["assignees"] = kwargs.get("assignees")
        captured["due_date_ms"] = kwargs.get("due_date_ms")
        return "CU-1"

    monkeypatch.setattr("meta_harness.webapp.routes_tickets.create_clickup_ticket", fake_create)

    client.post(
        "/api/tickets/create",
        json={
            "tickets": [
                {
                    "title": "Ticket A", "description": "d", "acceptance_criteria": [], "priority": "normal",
                    "assignee_user_id": 138194537, "due_date": "2026-08-24",
                },
            ]
        },
    )

    assert captured["assignees"] == [138194537]
    assert captured["due_date_ms"] is not None


def test_create_tickets_omits_assignees_and_due_date_when_absent(monkeypatch):
    captured = {}

    def fake_create(name, description, *, list_id=None, priority=None, project_path=None, **kwargs):
        captured["assignees"] = kwargs.get("assignees")
        captured["due_date_ms"] = kwargs.get("due_date_ms")
        return "CU-1"

    monkeypatch.setattr("meta_harness.webapp.routes_tickets.create_clickup_ticket", fake_create)

    client.post(
        "/api/tickets/create",
        json={"tickets": [{"title": "Ticket A", "description": "d", "acceptance_criteria": [], "priority": "normal"}]},
    )

    assert captured["assignees"] is None
    assert captured["due_date_ms"] is None


def test_verify_team_returns_verified_and_not_found(monkeypatch):
    monkeypatch.setattr(
        "meta_harness.webapp.routes_tickets.verify_team_emails",
        lambda emails: ([{"id": 1, "email": "alice@example.com", "username": "Alice"}], ["ghost@example.com"]),
    )

    response = client.post("/api/tickets/verify-team", json={"emails_text": "alice@example.com, ghost@example.com"})

    assert response.status_code == 200
    body = response.json()
    assert body["verified"] == [{"user_id": 1, "email": "alice@example.com", "username": "Alice"}]
    assert body["not_found"] == ["ghost@example.com"]


def test_generate_tickets_with_team_emails_assigns_members(monkeypatch):
    monkeypatch.setattr(
        "meta_harness.webapp.routes_tickets.generate_tickets_from_file",
        lambda filename, content, **kw: (
            [ProposedTicket(title="Do the thing", description="d", acceptance_criteria=[], category="mundane")],
            [],
        ),
    )
    monkeypatch.setattr(
        "meta_harness.webapp.routes_tickets.verify_team_emails",
        lambda emails: ([{"id": 1, "email": "alice@example.com", "username": "Alice"}], []),
    )

    response = client.post(
        "/api/tickets/generate",
        files={"file": ("spec.txt", b"some text", "text/plain")},
        data={"team_emails_text": "alice@example.com"},
    )

    assert response.status_code == 200
    ticket = response.json()["tickets"][0]
    assert ticket["assignee_user_id"] == 1
    assert ticket["assignee_email"] == "alice@example.com"


def test_generate_tickets_with_unmatched_team_emails_adds_warning(monkeypatch):
    monkeypatch.setattr(
        "meta_harness.webapp.routes_tickets.generate_tickets_from_file",
        lambda filename, content, **kw: (
            [ProposedTicket(title="Do the thing", description="d", acceptance_criteria=[], category="mundane")],
            [],
        ),
    )
    monkeypatch.setattr(
        "meta_harness.webapp.routes_tickets.verify_team_emails",
        lambda emails: ([], ["ghost@example.com"]),
    )

    response = client.post(
        "/api/tickets/generate",
        files={"file": ("spec.txt", b"some text", "text/plain")},
        data={"team_emails_text": "ghost@example.com"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["tickets"][0]["assignee_user_id"] is None
    assert any("ghost@example.com" in w for w in body["warnings"])


def test_generate_tickets_without_team_emails_leaves_tickets_unassigned(monkeypatch):
    monkeypatch.setattr(
        "meta_harness.webapp.routes_tickets.generate_tickets_from_file",
        lambda filename, content, **kw: (
            [ProposedTicket(title="Do the thing", description="d", acceptance_criteria=[], category="mundane")],
            [],
        ),
    )

    def boom(emails):
        raise AssertionError("must not verify team emails when none were given")

    monkeypatch.setattr("meta_harness.webapp.routes_tickets.verify_team_emails", boom)

    response = client.post("/api/tickets/generate", files={"file": ("spec.txt", b"some text", "text/plain")})

    assert response.status_code == 200
    assert response.json()["tickets"][0]["assignee_user_id"] is None
