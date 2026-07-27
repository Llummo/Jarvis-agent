from fastapi.testclient import TestClient

from meta_harness.webapp import progress
from meta_harness.webapp.app import app

client = TestClient(app)


def test_get_progress_unknown_token_returns_empty_steps():
    response = client.get("/api/progress/does-not-exist")

    assert response.status_code == 200
    assert response.json()["steps"] == []


def test_get_progress_returns_pushed_steps_in_order():
    progress.start("tok1")
    progress.push("tok1", "Fetching ticket T1 from ClickUp…")
    progress.push("tok1", "Claude is analyzing the ticket…")

    response = client.get("/api/progress/tok1")

    assert response.json()["steps"] == [
        "Fetching ticket T1 from ClickUp…",
        "Claude is analyzing the ticket…",
    ]


def test_start_clears_any_previous_steps_for_the_same_token():
    progress.start("tok2")
    progress.push("tok2", "first run step")
    progress.start("tok2")

    assert progress.get("tok2") == []


def test_post_review_with_progress_token_records_steps(monkeypatch):
    from meta_harness.qa_flow import QATicketReview
    from meta_harness.run_archive import RunRecord

    def fake_review_and_record(ticket_id, *, on_step=None, **kw):
        on_step("Fetching ticket T1 from ClickUp…")
        on_step("Claude is analyzing the ticket…")
        review = QATicketReview(ticket_id=ticket_id, ticket_name="Fix login bug", observation="Check", severity="minor")
        record = RunRecord(run_id="run1", agent="qa_ticket_review", subject_id=ticket_id, started_at="t", ok=True)
        return review, None, record

    monkeypatch.setattr("meta_harness.webapp.routes_qa_flow.review_and_record", fake_review_and_record)

    response = client.post(
        "/api/qa/reviews",
        json={"ticket_id": "T1", "project": "sigo-front", "progress_token": "tok3"},
    )

    assert response.status_code == 200
    assert progress.get("tok3") == [
        "Fetching ticket T1 from ClickUp…",
        "Claude is analyzing the ticket…",
    ]


def test_post_review_without_progress_token_does_not_touch_progress_store(monkeypatch):
    from meta_harness.qa_flow import QATicketReview
    from meta_harness.run_archive import RunRecord

    def fake_review_and_record(ticket_id, *, on_step=None, **kw):
        assert on_step is None
        review = QATicketReview(ticket_id=ticket_id, ticket_name="Fix login bug", observation="Check", severity="minor")
        record = RunRecord(run_id="run1", agent="qa_ticket_review", subject_id=ticket_id, started_at="t", ok=True)
        return review, None, record

    monkeypatch.setattr("meta_harness.webapp.routes_qa_flow.review_and_record", fake_review_and_record)

    response = client.post("/api/qa/reviews", json={"ticket_id": "T1", "project": "sigo-front"})

    assert response.status_code == 200


def test_post_generate_tickets_with_progress_token_records_steps(monkeypatch):
    from meta_harness.ticket_generator import ProposedTicket

    def fake_generate(filename, content, *, on_step=None, **kw):
        on_step(f'Extracting text from "{filename}"…')
        on_step("Sending document to Claude for ticket extraction — this can take a minute…")
        return (
            [ProposedTicket(title="Do the thing", description="d", acceptance_criteria=[], category="mundane")],
            [],
        )

    monkeypatch.setattr("meta_harness.webapp.routes_tickets.generate_tickets_from_file", fake_generate)

    response = client.post(
        "/api/tickets/generate",
        files={"file": ("spec.txt", b"some text", "text/plain")},
        data={"progress_token": "tok4"},
    )

    assert response.status_code == 200
    assert progress.get("tok4") == [
        'Extracting text from "spec.txt"…',
        "Sending document to Claude for ticket extraction — this can take a minute…",
    ]
