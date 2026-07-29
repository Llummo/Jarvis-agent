from datetime import date

from fastapi.testclient import TestClient

from meta_harness.clickup_bridge import ClickUpReadError, ClickUpTicketError
from meta_harness.image_input import MAX_IMAGES, ImageDescriptionError
from meta_harness.ticket_generator import (
    CHAT_INLINE_TICKET_LIMIT,
    AcceptanceCriterion,
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
                    description="desc",
                    acceptance_criteria=[AcceptanceCriterion(name="C1", given="a", when="b", then="c")],
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
    assert ticket["title"] == "TAS-01 | Build login page"
    assert ticket["user_story"] == "As a user, I want to log in, so I can access my account."
    assert ticket["description"] == "desc"
    assert ticket["acceptance_criteria"][0]["name"] == "C1"
    assert ticket["acceptance_criteria"][0]["given"] == "a"
    assert ticket["priority"] == "high"
    assert ticket["category"] == "backend"
    assert ticket["due_date"] is not None  # apply_sprint_due_dates always sets this
    assert body["warnings"] == ["some warning"]


def test_generate_tickets_numbers_in_one_sequence_from_the_start_number(monkeypatch):
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
        data={"start_number": "2"},
    )

    assert response.status_code == 200
    titles = [t["title"] for t in response.json()["tickets"]]
    # One shared sequence regardless of category, continuing from 2.
    assert titles == [
        "TAS-02 | General planning",
        "TAS-03 | Build API endpoint",
        "TAS-04 | Build login page",
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

    assert response.json()["tickets"][0]["title"] == "TAS-01 | Set up CI pipeline"


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
                    "acceptance_criteria": [
                        {"name": "Uno", "given": "g1", "when": "w1", "then": "t1"},
                        {"name": "Dos", "given": "g2", "when": "w2", "then": "t2"},
                    ],
                    "priority": "normal",
                },
            ]
        },
    )

    assert "Do the thing." in captured["description"]
    assert "Uno: Dado que g1, cuando w1, entonces t1." in captured["description"]
    assert "Dos: Dado que g2, cuando w2, entonces t2." in captured["description"]


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

    assert "📝 DESCRIPCIÓN\nDo the thing." in captured["description"]


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
                    "assignee_clickup_id": 138194537, "due_date": "2026-08-24",
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
    monkeypatch.setattr("meta_harness.webapp.routes_tickets.list_team_members", lambda **kw: [])
    monkeypatch.setattr(
        "meta_harness.webapp.routes_tickets.verify_team_emails",
        lambda emails, **kw: ([{"clickup_id": 1, "email": "alice@example.com", "username": "Alice"}], ["ghost@example.com"]),
    )

    response = client.post("/api/tickets/verify-team", json={"emails_text": "alice@example.com, ghost@example.com"})

    assert response.status_code == 200
    body = response.json()
    assert body["verified"] == [{"email": "alice@example.com", "username": "Alice", "clickup_id": 1, "linear_id": None}]
    assert body["not_found"] == ["ghost@example.com"]


def test_generate_tickets_with_team_emails_assigns_members(monkeypatch):
    monkeypatch.setattr(
        "meta_harness.webapp.routes_tickets.generate_tickets_from_file",
        lambda filename, content, **kw: (
            [ProposedTicket(title="Do the thing", description="d", acceptance_criteria=[], category="mundane")],
            [],
        ),
    )
    monkeypatch.setattr("meta_harness.webapp.routes_tickets.list_team_members", lambda **kw: [])
    monkeypatch.setattr(
        "meta_harness.webapp.routes_tickets.verify_team_emails",
        lambda emails, **kw: ([{"clickup_id": 1, "email": "alice@example.com", "username": "Alice"}], []),
    )

    response = client.post(
        "/api/tickets/generate",
        files={"file": ("spec.txt", b"some text", "text/plain")},
        data={"team_emails_text": "alice@example.com"},
    )

    assert response.status_code == 200
    ticket = response.json()["tickets"][0]
    assert ticket["assignee_clickup_id"] == 1
    assert ticket["assignee_email"] == "alice@example.com"


def test_generate_tickets_with_unmatched_team_emails_adds_warning(monkeypatch):
    monkeypatch.setattr(
        "meta_harness.webapp.routes_tickets.generate_tickets_from_file",
        lambda filename, content, **kw: (
            [ProposedTicket(title="Do the thing", description="d", acceptance_criteria=[], category="mundane")],
            [],
        ),
    )
    monkeypatch.setattr("meta_harness.webapp.routes_tickets.list_team_members", lambda **kw: [])
    monkeypatch.setattr(
        "meta_harness.webapp.routes_tickets.verify_team_emails",
        lambda emails, **kw: ([], ["ghost@example.com"]),
    )

    response = client.post(
        "/api/tickets/generate",
        files={"file": ("spec.txt", b"some text", "text/plain")},
        data={"team_emails_text": "ghost@example.com"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["tickets"][0]["assignee_clickup_id"] is None
    assert any("ghost@example.com" in w for w in body["warnings"])


def test_generate_tickets_without_team_emails_leaves_tickets_unassigned(monkeypatch):
    monkeypatch.setattr(
        "meta_harness.webapp.routes_tickets.generate_tickets_from_file",
        lambda filename, content, **kw: (
            [ProposedTicket(title="Do the thing", description="d", acceptance_criteria=[], category="mundane")],
            [],
        ),
    )

    def boom(emails, **kw):
        raise AssertionError("must not verify team emails when none were given")

    monkeypatch.setattr("meta_harness.webapp.routes_tickets.verify_team_emails", boom)

    response = client.post("/api/tickets/generate", files={"file": ("spec.txt", b"some text", "text/plain")})

    assert response.status_code == 200
    assert response.json()["tickets"][0]["assignee_clickup_id"] is None


def test_generate_tickets_with_project_dates_compresses_due_dates(monkeypatch):
    monkeypatch.setattr(
        "meta_harness.webapp.routes_tickets.generate_tickets_from_file",
        lambda filename, content, **kw: (
            [ProposedTicket(title="t1", description="d", acceptance_criteria=[], category="mundane", sprint=6)],
            [],
        ),
    )

    response = client.post(
        "/api/tickets/generate",
        files={"file": ("spec.txt", b"some text", "text/plain")},
        data={"project_start": "2026-07-27", "project_end": "2026-08-24"},
    )

    assert response.status_code == 200
    assert response.json()["tickets"][0]["due_date"] == "2026-08-24"


def test_generate_tickets_passes_project_dates_to_generator(monkeypatch):
    captured = {}

    def fake_generate(filename, content, *, on_step=None, project_start=None, project_end=None, **kw):
        captured["project_start"] = project_start
        captured["project_end"] = project_end
        return ([ProposedTicket(title="t1", description="d", acceptance_criteria=[])], [])

    monkeypatch.setattr("meta_harness.webapp.routes_tickets.generate_tickets_from_file", fake_generate)

    client.post(
        "/api/tickets/generate",
        files={"file": ("spec.txt", b"some text", "text/plain")},
        data={"project_start": "2026-07-27", "project_end": "2026-08-24"},
    )

    from datetime import date
    assert captured["project_start"] == date(2026, 7, 27)
    assert captured["project_end"] == date(2026, 8, 24)


def test_generate_tickets_rejects_end_date_before_start_date():
    response = client.post(
        "/api/tickets/generate",
        files={"file": ("spec.txt", b"some text", "text/plain")},
        data={"project_start": "2026-08-24", "project_end": "2026-07-27"},
    )

    assert response.status_code == 400


def test_generate_tickets_rejects_invalid_date_format():
    response = client.post(
        "/api/tickets/generate",
        files={"file": ("spec.txt", b"some text", "text/plain")},
        data={"project_end": "not-a-date"},
    )

    assert response.status_code == 400


# ---------------------------------------------------------------------------
# /api/tickets/from-idea — the chat generation path
# ---------------------------------------------------------------------------


def test_from_idea_returns_numbered_tickets(monkeypatch):
    monkeypatch.setattr(
        "meta_harness.webapp.routes_tickets.generate_tickets_from_idea",
        lambda idea, **kw: (
            [ProposedTicket(title="Exportar candidatos", description="d", acceptance_criteria=[], category="backend")],
            [],
        ),
    )

    response = client.post("/api/tickets/from-idea", json={"idea": "exportar candidatos a excel"})

    assert response.status_code == 200
    assert response.json()["tickets"][0]["title"] == "TAS-01 | Exportar candidatos"


def test_from_idea_passes_idea_and_history(monkeypatch):
    captured = {}

    def fake_generate(idea, **kw):
        captured["idea"] = idea
        captured["history"] = kw.get("history")
        return ([ProposedTicket(title="t", description="d", acceptance_criteria=[])], [])

    monkeypatch.setattr("meta_harness.webapp.routes_tickets.generate_tickets_from_idea", fake_generate)

    client.post(
        "/api/tickets/from-idea",
        json={"idea": "sepáralo en dos", "history": [{"role": "user", "content": "exportar candidatos"}]},
    )

    assert captured["idea"] == "sepáralo en dos"
    assert captured["history"] == [{"role": "user", "content": "exportar candidatos"}]


def test_from_idea_respects_the_start_number(monkeypatch):
    monkeypatch.setattr(
        "meta_harness.webapp.routes_tickets.generate_tickets_from_idea",
        lambda idea, **kw: (
            [ProposedTicket(title="Exportar", description="d", acceptance_criteria=[], category="backend")],
            [],
        ),
    )

    response = client.post(
        "/api/tickets/from-idea", json={"idea": "exportar", "start_number": 7}
    )

    assert response.json()["tickets"][0]["title"] == "TAS-07 | Exportar"


def test_from_idea_parse_error_returns_502(monkeypatch):
    def boom(idea, **kw):
        raise TicketParseError("no usable tickets")

    monkeypatch.setattr("meta_harness.webapp.routes_tickets.generate_tickets_from_idea", boom)

    response = client.post("/api/tickets/from-idea", json={"idea": "algo"})

    assert response.status_code == 502


def test_from_idea_claude_missing_returns_503(monkeypatch):
    def boom(idea, **kw):
        raise ClaudeNotFoundError("no claude binary")

    monkeypatch.setattr("meta_harness.webapp.routes_tickets.generate_tickets_from_idea", boom)

    response = client.post("/api/tickets/from-idea", json={"idea": "algo"})

    assert response.status_code == 503


# ---------------------------------------------------------------------------
# /api/tickets/chat — the one generation entry point (message and/or document)
# ---------------------------------------------------------------------------


def _one_ticket(title="Exportar candidatos", category="backend"):
    return [ProposedTicket(title=title, description="d", acceptance_criteria=[], category=category)]


def test_chat_generates_from_a_typed_message(monkeypatch):
    captured = {}

    def fake_generate(idea, **kw):
        captured["idea"] = idea
        captured["history"] = kw.get("history")
        return (_one_ticket(), [])

    monkeypatch.setattr("meta_harness.webapp.routes_tickets.generate_tickets_from_idea", fake_generate)

    response = client.post(
        "/api/tickets/chat",
        data={"idea": "exportar candidatos", "history": '[{"role": "user", "content": "hola"}]'},
    )

    assert response.status_code == 200
    assert captured["idea"] == "exportar candidatos"
    assert captured["history"] == [{"role": "user", "content": "hola"}]
    assert response.json()["tickets"][0]["title"] == "TAS-01 | Exportar candidatos"
    assert response.json()["source_document"] is None


def test_chat_generates_from_an_attached_document(monkeypatch):
    captured = {}

    def fake_generate(text, **kw):
        captured["text"] = text
        return (_one_ticket(), [])

    monkeypatch.setattr("meta_harness.webapp.routes_tickets.generate_tickets_from_text", fake_generate)

    response = client.post(
        "/api/tickets/chat",
        files={"file": ("requisitos.md", "# Requisitos\nExportar candidatos".encode(), "text/markdown")},
    )

    assert response.status_code == 200
    assert "Exportar candidatos" in captured["text"]
    assert response.json()["source_document"] == "requisitos.md"


def test_chat_message_sent_with_a_document_steers_the_analysis(monkeypatch):
    captured = {}

    def fake_generate(text, **kw):
        captured["text"] = text
        return (_one_ticket(), [])

    monkeypatch.setattr("meta_harness.webapp.routes_tickets.generate_tickets_from_text", fake_generate)

    client.post(
        "/api/tickets/chat",
        data={"idea": "solo el modulo de talento"},
        files={"file": ("requisitos.md", b"# Requisitos", "text/markdown")},
    )

    assert "# Requisitos" in captured["text"]
    assert "solo el modulo de talento" in captured["text"], (
        "a message sent with a document must reach the model, not be dropped"
    )


def test_chat_with_a_document_never_calls_the_idea_generator(monkeypatch):
    def boom(*args, **kw):
        raise AssertionError("a document must go through the document generator")

    monkeypatch.setattr("meta_harness.webapp.routes_tickets.generate_tickets_from_idea", boom)
    monkeypatch.setattr(
        "meta_harness.webapp.routes_tickets.generate_tickets_from_text",
        lambda text, **kw: (_one_ticket(), []),
    )

    response = client.post(
        "/api/tickets/chat",
        data={"idea": "y agrega validaciones"},
        files={"file": ("requisitos.md", b"# Requisitos", "text/markdown")},
    )

    assert response.status_code == 200


def test_chat_with_neither_message_nor_document_returns_400():
    response = client.post("/api/tickets/chat", data={"idea": "   "})

    assert response.status_code == 400


def test_chat_rejects_unparseable_history():
    response = client.post("/api/tickets/chat", data={"idea": "algo", "history": "not json"})

    assert response.status_code == 400


def test_chat_flags_overflow_past_the_inline_limit(monkeypatch):
    many = [
        ProposedTicket(title=f"T{i}", description="d", acceptance_criteria=[], category="backend")
        for i in range(CHAT_INLINE_TICKET_LIMIT + 1)
    ]
    monkeypatch.setattr(
        "meta_harness.webapp.routes_tickets.generate_tickets_from_idea", lambda idea, **kw: (many, [])
    )

    response = client.post("/api/tickets/chat", data={"idea": "algo grande"})

    assert response.json()["overflow"] is True


def test_chat_does_not_flag_overflow_at_the_inline_limit(monkeypatch):
    few = [
        ProposedTicket(title=f"T{i}", description="d", acceptance_criteria=[], category="backend")
        for i in range(CHAT_INLINE_TICKET_LIMIT)
    ]
    monkeypatch.setattr(
        "meta_harness.webapp.routes_tickets.generate_tickets_from_idea", lambda idea, **kw: (few, [])
    )

    response = client.post("/api/tickets/chat", data={"idea": "algo corto"})

    assert response.json()["overflow"] is False


def test_chat_respects_the_start_number(monkeypatch):
    monkeypatch.setattr(
        "meta_harness.webapp.routes_tickets.generate_tickets_from_idea",
        lambda idea, **kw: (_one_ticket("Exportar"), []),
    )

    response = client.post("/api/tickets/chat", data={"idea": "exportar", "start_number": 7})

    assert response.json()["tickets"][0]["title"] == "TAS-07 | Exportar"


def test_chat_numbers_every_ticket_with_the_shared_tas_prefix(monkeypatch):
    # The regression that broke generation outright: /chat kept calling the
    # per-category numbering that the TAS change had already deleted.
    monkeypatch.setattr(
        "meta_harness.webapp.routes_tickets.generate_tickets_from_idea",
        lambda idea, **kw: (
            _one_ticket("Endpoint", "backend") + _one_ticket("Pantalla", "frontend"),
            [],
        ),
    )

    titles = [t["title"] for t in client.post("/api/tickets/chat", data={"idea": "x"}).json()["tickets"]]

    assert titles == ["TAS-01 | Endpoint", "TAS-02 | Pantalla"]


def test_chat_assigns_verified_team_members(monkeypatch):
    monkeypatch.setattr(
        "meta_harness.webapp.routes_tickets.generate_tickets_from_idea",
        lambda idea, **kw: (_one_ticket(), []),
    )
    monkeypatch.setattr(
        "meta_harness.webapp.routes_tickets.list_team_members",
        lambda **kw: [{"clickup_id": 1, "email": "alice@example.com", "username": "Alice"}],
    )

    response = client.post(
        "/api/tickets/chat",
        data={"idea": "exportar", "team_emails_text": "alice@example.com"},
    )

    assert response.json()["tickets"][0]["assignee_email"] == "alice@example.com"


def test_chat_reports_team_emails_that_match_nobody(monkeypatch):
    monkeypatch.setattr(
        "meta_harness.webapp.routes_tickets.generate_tickets_from_idea",
        lambda idea, **kw: (_one_ticket(), []),
    )
    monkeypatch.setattr("meta_harness.webapp.routes_tickets.list_team_members", lambda **kw: [])

    response = client.post(
        "/api/tickets/chat", data={"idea": "exportar", "team_emails_text": "ghost@example.com"}
    )

    assert response.status_code == 200
    assert any("ghost@example.com" in w for w in response.json()["warnings"])


def test_chat_passes_the_project_window_to_the_document_generator(monkeypatch):
    captured = {}

    def fake_generate(text, **kw):
        captured.update(start=kw.get("project_start"), end=kw.get("project_end"))
        return (_one_ticket(), [])

    monkeypatch.setattr("meta_harness.webapp.routes_tickets.generate_tickets_from_text", fake_generate)

    client.post(
        "/api/tickets/chat",
        data={"project_start": "2026-07-01", "project_end": "2026-08-01"},
        files={"file": ("requisitos.md", b"# Requisitos", "text/markdown")},
    )

    assert captured["start"] == date(2026, 7, 1)
    assert captured["end"] == date(2026, 8, 1)


def test_chat_rejects_an_end_date_before_the_start_date():
    response = client.post(
        "/api/tickets/chat",
        data={"idea": "algo", "project_start": "2026-08-24", "project_end": "2026-07-27"},
    )

    assert response.status_code == 400


def test_chat_unsupported_document_returns_400(monkeypatch):
    response = client.post(
        "/api/tickets/chat", files={"file": ("spec.docx", b"binary", "application/octet-stream")}
    )

    assert response.status_code == 400


def test_chat_parse_error_returns_502(monkeypatch):
    def boom(idea, **kw):
        raise TicketParseError("no usable tickets")

    monkeypatch.setattr("meta_harness.webapp.routes_tickets.generate_tickets_from_idea", boom)

    response = client.post("/api/tickets/chat", data={"idea": "algo"})

    assert response.status_code == 502


def test_chat_claude_missing_returns_503(monkeypatch):
    def boom(idea, **kw):
        raise ClaudeNotFoundError("no claude binary")

    monkeypatch.setattr("meta_harness.webapp.routes_tickets.generate_tickets_from_idea", boom)

    response = client.post("/api/tickets/chat", data={"idea": "algo"})

    assert response.status_code == 503


# ---------------------------------------------------------------------------
# /api/tickets/chat — pasted images
# ---------------------------------------------------------------------------


def _png_bytes() -> bytes:
    import struct
    import zlib

    raw = b"".join(b"\x00" + bytes([255, 255, 255] * 4) for _ in range(4))

    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _stub_description(monkeypatch, text="Una pantalla de alta de persona con campos nombre y email."):
    monkeypatch.setattr("meta_harness.webapp.routes_tickets.describe_images", lambda paths, **kw: text)


def test_chat_accepts_a_pasted_image_with_no_text_at_all(monkeypatch):
    _stub_description(monkeypatch)
    monkeypatch.setattr(
        "meta_harness.webapp.routes_tickets.generate_tickets_from_idea",
        lambda idea, **kw: (_one_ticket(), []),
    )

    response = client.post(
        "/api/tickets/chat", files={"images": ("pasted.png", _png_bytes(), "image/png")}
    )

    assert response.status_code == 200


def test_chat_feeds_the_image_description_into_generation(monkeypatch):
    captured = {}
    _stub_description(monkeypatch, "Un formulario con los campos nombre, email y telefono.")

    def fake_generate(idea, **kw):
        captured["idea"] = idea
        return (_one_ticket(), [])

    monkeypatch.setattr("meta_harness.webapp.routes_tickets.generate_tickets_from_idea", fake_generate)

    client.post(
        "/api/tickets/chat",
        data={"idea": "haz el alta de personas"},
        files={"images": ("pasted.png", _png_bytes(), "image/png")},
    )

    assert "haz el alta de personas" in captured["idea"]
    assert "nombre, email y telefono" in captured["idea"], (
        "what the image showed must reach the generator"
    )


def test_chat_appends_the_image_description_to_an_attached_document(monkeypatch):
    captured = {}
    _stub_description(monkeypatch, "Un diagrama de flujo de aprobacion.")
    monkeypatch.setattr(
        "meta_harness.webapp.routes_tickets.generate_tickets_from_text",
        lambda text, **kw: (captured.update(text=text) or (_one_ticket(), [])),
    )

    client.post(
        "/api/tickets/chat",
        files=[
            ("file", ("requisitos.md", b"# Requisitos", "text/markdown")),
            ("images", ("pasted.png", _png_bytes(), "image/png")),
        ],
    )

    assert "# Requisitos" in captured["text"]
    assert "diagrama de flujo de aprobacion" in captured["text"]


def test_chat_generation_stays_tool_free_when_images_are_pasted(monkeypatch):
    # The whole point of the two-stage split: only the describing call may
    # touch the filesystem, and it is never the call that drafts tickets.
    _stub_description(monkeypatch)
    captured = {}
    monkeypatch.setattr(
        "meta_harness.webapp.routes_tickets.generate_tickets_from_idea",
        lambda idea, **kw: (captured.update(kwargs=kw) or (_one_ticket(), [])),
    )

    client.post("/api/tickets/chat", files={"images": ("pasted.png", _png_bytes(), "image/png")})

    assert "image_paths" not in captured["kwargs"]
    assert "allowed_tools" not in captured["kwargs"]


def test_chat_without_images_never_invokes_the_vision_call(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("a text-only message must not pay for a vision call")

    monkeypatch.setattr("meta_harness.webapp.routes_tickets.describe_images", boom)
    monkeypatch.setattr(
        "meta_harness.webapp.routes_tickets.generate_tickets_from_idea",
        lambda idea, **kw: (_one_ticket(), []),
    )

    assert client.post("/api/tickets/chat", data={"idea": "algo"}).status_code == 200


def test_chat_rejects_a_pasted_file_that_is_not_an_image():
    response = client.post(
        "/api/tickets/chat",
        data={"idea": "algo"},
        files={"images": ("payload.png", b"#!/bin/sh\nrm -rf /", "image/png")},
    )

    assert response.status_code == 400


def test_chat_rejects_more_images_than_the_cap():
    files = [("images", (f"p{i}.png", _png_bytes(), "image/png")) for i in range(MAX_IMAGES + 1)]

    response = client.post("/api/tickets/chat", data={"idea": "algo"}, files=files)

    assert response.status_code == 400


def test_chat_image_description_failure_returns_502(monkeypatch):
    def boom(paths, **kw):
        raise ImageDescriptionError("vision unavailable")

    monkeypatch.setattr("meta_harness.webapp.routes_tickets.describe_images", boom)

    response = client.post(
        "/api/tickets/chat", files={"images": ("pasted.png", _png_bytes(), "image/png")}
    )

    assert response.status_code == 502


def test_chat_still_rejects_a_request_with_nothing_in_it_at_all():
    assert client.post("/api/tickets/chat", data={"idea": "   "}).status_code == 400


# ---------------------------------------------------------------------------
# /api/tickets/team-members — auto-detected, assignable-in-this-tracker roster
# ---------------------------------------------------------------------------


def test_team_members_returns_clickup_roster_by_default(monkeypatch):
    monkeypatch.setattr(
        "meta_harness.webapp.routes_tickets.list_team_members",
        lambda **kw: [{"clickup_id": 1, "email": "alice@example.com", "username": "Alice"}],
    )

    response = client.get("/api/tickets/team-members")

    assert response.status_code == 200
    assert response.json() == [
        {"email": "alice@example.com", "username": "Alice", "clickup_id": 1, "linear_id": None}
    ]


def test_team_members_for_linear_only_returns_members_with_a_linear_id(monkeypatch):
    monkeypatch.setattr(
        "meta_harness.webapp.routes_tickets.list_team_members",
        lambda **kw: [{"clickup_id": 1, "email": "alice@example.com", "username": "Alice"}],
    )
    monkeypatch.setattr(
        "meta_harness.webapp.routes_tickets.list_linear_members",
        lambda team_id, **kw: [{"id": "uuid-b", "name": "Bob", "email": "bob@example.com"}],
    )

    response = client.get("/api/tickets/team-members", params={"tracker": "linear", "linear_team_id": "T1"})

    assert response.status_code == 200
    # Alice has no Linear id, so she is not assignable in Linear and is excluded.
    assert [m["email"] for m in response.json()] == ["bob@example.com"]
    assert response.json()[0]["linear_id"] == "uuid-b"


def test_team_members_for_clickup_excludes_linear_only_people(monkeypatch):
    monkeypatch.setattr(
        "meta_harness.webapp.routes_tickets.list_team_members",
        lambda **kw: [{"clickup_id": 1, "email": "alice@example.com", "username": "Alice"}],
    )
    monkeypatch.setattr(
        "meta_harness.webapp.routes_tickets.list_linear_members",
        lambda team_id, **kw: [{"id": "uuid-b", "name": "Bob", "email": "bob@example.com"}],
    )

    response = client.get("/api/tickets/team-members", params={"tracker": "clickup", "linear_team_id": "T1"})

    assert [m["email"] for m in response.json()] == ["alice@example.com"]


def test_team_members_survives_clickup_being_unreachable(monkeypatch):
    def boom(**kw):
        raise ClickUpReadError("clickup is down")

    monkeypatch.setattr("meta_harness.webapp.routes_tickets.list_team_members", boom)
    monkeypatch.setattr(
        "meta_harness.webapp.routes_tickets.list_linear_members",
        lambda team_id, **kw: [{"id": "uuid-b", "name": "Bob", "email": "bob@example.com"}],
    )

    response = client.get("/api/tickets/team-members", params={"tracker": "linear", "linear_team_id": "T1"})

    assert response.status_code == 200
    assert [m["email"] for m in response.json()] == ["bob@example.com"]


# ---------------------------------------------------------------------------
# /api/tickets/module-relevance
# ---------------------------------------------------------------------------


def test_module_relevance_returns_the_verdict(monkeypatch):
    from meta_harness.module_relevance import ModuleRelevance

    monkeypatch.setattr(
        "meta_harness.webapp.routes_tickets.analyze_module_relevance",
        lambda ticket_id, **kw: ModuleRelevance(
            ticket_id=ticket_id, ticket_name="Editar ficha", module_name=kw["module_name"],
            verdict="related", confidence=0.87, rationale="Encaja con el módulo.",
            matched_aspects=["Ficha de persona"], module_gaps=[],
        ),
    )

    response = client.post(
        "/api/tickets/module-relevance",
        json={"ticket_id": "T1", "module_name": "Personas", "module_context": "doc oficial"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["verdict"] == "related"
    assert body["confidence"] == 0.87
    assert body["matched_aspects"] == ["Ficha de persona"]


def test_module_relevance_passes_tracker_through(monkeypatch):
    from meta_harness.module_relevance import ModuleRelevance

    captured = {}

    def fake(ticket_id, **kw):
        captured.update(kw)
        return ModuleRelevance(
            ticket_id=ticket_id, ticket_name="x", module_name="Personas", verdict="unrelated",
            confidence=0.1, rationale="No.", matched_aspects=[], module_gaps=["Otro módulo"],
        )

    monkeypatch.setattr("meta_harness.webapp.routes_tickets.analyze_module_relevance", fake)

    client.post(
        "/api/tickets/module-relevance",
        json={"ticket_id": "I1", "module_name": "Personas", "module_context": "doc", "tracker": "linear"},
    )

    assert captured["tracker"] == "linear"
    assert captured["module_context"] == "doc"


def test_module_relevance_missing_context_returns_400(monkeypatch):
    def boom(ticket_id, **kw):
        raise ValueError("module_context is required")

    monkeypatch.setattr("meta_harness.webapp.routes_tickets.analyze_module_relevance", boom)

    response = client.post(
        "/api/tickets/module-relevance",
        json={"ticket_id": "T1", "module_name": "Personas", "module_context": ""},
    )

    assert response.status_code == 400


def test_module_relevance_bulk_lists_aligned_tickets_first(monkeypatch):
    from meta_harness.module_relevance import ModuleRelevance

    def make(ticket_id, verdict, confidence, name):
        return ModuleRelevance(
            ticket_id=ticket_id, ticket_name=name, module_name="Personas", verdict=verdict,
            confidence=confidence, rationale="Motivo.", matched_aspects=[], module_gaps=[],
        )

    monkeypatch.setattr(
        "meta_harness.webapp.routes_tickets.analyze_modules_bulk",
        lambda ticket_ids, **kw: [
            ("T1", make("T1", "unrelated", 0.9, "Asistencia"), None),
            ("T2", make("T2", "related", 0.95, "Ficha de persona"), None),
            ("T3", None, "no se pudo leer"),
        ],
    )

    response = client.post(
        "/api/tickets/module-relevance/bulk",
        json={"ticket_ids": ["T1", "T2", "T3"], "module_name": "Personas", "module_context": "doc"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["summary"] == {
        "analyzed": 2, "related": 1, "partially_related": 0, "unrelated": 1, "failed": 1
    }
    # "aligned" is the direct answer to which tickets belong to the module
    assert [r["ticket_name"] for r in body["aligned"]] == ["Ficha de persona"]
    # full results are ordered aligned-first, errors last
    assert [r["ticket_id"] for r in body["results"]] == ["T2", "T1", "T3"]
    assert body["results"][2]["error"] == "no se pudo leer"
    assert "# Módulo: Personas" in body["report_markdown"]


def test_module_relevance_bulk_passes_tracker_and_context(monkeypatch):
    captured = {}

    def fake(ticket_ids, **kw):
        captured.update(kw)
        captured["ticket_ids"] = list(ticket_ids)
        return []

    monkeypatch.setattr("meta_harness.webapp.routes_tickets.analyze_modules_bulk", fake)

    client.post(
        "/api/tickets/module-relevance/bulk",
        json={
            "ticket_ids": ["I1", "I2"], "module_name": "Personas",
            "module_context": "doc oficial", "tracker": "linear",
        },
    )

    assert captured["ticket_ids"] == ["I1", "I2"]
    assert captured["tracker"] == "linear"
    assert captured["module_context"] == "doc oficial"
