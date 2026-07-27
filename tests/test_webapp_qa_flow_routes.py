from fastapi.testclient import TestClient

from meta_harness.clickup_bridge import ClickUpReadError
from meta_harness.qa_findings import QAFinding, QAFindingStore
from meta_harness.qa_flow import ClaudeNotFoundError, QATicketReview
from meta_harness.run_archive import RunArchive, RunRecord, RunStepRecord
from meta_harness.webapp.app import app
from meta_harness.webapp.deps import get_qa_store

client = TestClient(app)


def _client_with_store(tmp_path) -> TestClient:
    store = QAFindingStore(tmp_path / "findings.db")
    app.dependency_overrides[get_qa_store] = lambda: store
    return TestClient(app)


def _review():
    return QATicketReview(ticket_id="T1", ticket_name="Fix login bug", observation="Check login", severity="major")


def test_post_review_dry_run(monkeypatch):
    monkeypatch.setattr(
        "meta_harness.webapp.routes_qa_flow.review_and_record",
        lambda ticket_id, **kw: (
            _review(),
            None,
            RunRecord(run_id="run1", agent="qa_ticket_review", subject_id=ticket_id, started_at="t", ok=True),
        ),
    )

    response = client.post("/api/qa/reviews", json={"ticket_id": "T1", "project": "sigo-front"})

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == "run1"
    assert body["review"]["severity"] == "major"
    assert body["finding"] is None


def test_post_review_persist_returns_finding(monkeypatch):
    finding = QAFinding(
        id=1, project="sigo-front", route="Fix login bug", observation="Check login",
        severity="major", status="open", created_at="t", updated_at="t",
    )
    monkeypatch.setattr(
        "meta_harness.webapp.routes_qa_flow.review_and_record",
        lambda ticket_id, **kw: (
            _review(), finding,
            RunRecord(run_id="run1", agent="qa_ticket_review", subject_id=ticket_id, started_at="t", ok=True),
        ),
    )

    response = client.post("/api/qa/reviews", json={"ticket_id": "T1", "project": "sigo-front", "persist": True})

    assert response.status_code == 200
    assert response.json()["finding"]["id"] == 1


def test_post_review_clickup_error_returns_502(monkeypatch):
    def boom(ticket_id, **kw):
        raise ClickUpReadError("ticket not found")

    monkeypatch.setattr("meta_harness.webapp.routes_qa_flow.review_and_record", boom)

    response = client.post("/api/qa/reviews", json={"ticket_id": "T1", "project": "sigo-front"})

    assert response.status_code == 502
    assert "ticket not found" in response.json()["detail"]


def test_post_review_claude_not_found_returns_502(monkeypatch):
    def boom(ticket_id, **kw):
        raise ClaudeNotFoundError("no claude")

    monkeypatch.setattr("meta_harness.webapp.routes_qa_flow.review_and_record", boom)

    response = client.post("/api/qa/reviews", json={"ticket_id": "T1", "project": "sigo-front"})

    assert response.status_code == 502


def test_get_reviews_lists_recorded_runs(tmp_path, monkeypatch):
    archive = RunArchive("qa_ticket_review", directory=tmp_path)
    archive.append(RunRecord(
        run_id="run1", agent="qa_ticket_review", subject_id="T1", started_at="t1", ok=True,
        steps=[RunStepRecord(command=["review"], returncode=0, stdout="{}", stderr="")],
    ))
    monkeypatch.setattr("meta_harness.webapp.routes_qa_flow.RunArchive", lambda name: archive)

    response = client.get("/api/qa/reviews")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["run_id"] == "run1"
    assert body[0]["ticket_id"] == "T1"


def test_post_replay_dry_run(monkeypatch):
    monkeypatch.setattr(
        "meta_harness.webapp.routes_qa_flow.replay_qa_review",
        lambda run_id, **kw: (
            RunRecord(run_id=run_id, agent="qa_ticket_review", subject_id="T1", started_at="t", ok=True),
            _review(), None,
            RunRecord(run_id="run2", agent="qa_ticket_review", subject_id="T1", started_at="t2", ok=True),
        ),
    )

    response = client.post("/api/qa/reviews/run1/replay", json={})

    assert response.status_code == 200
    assert response.json()["run_id"] == "run2"


def test_post_replay_missing_run_id_returns_404(monkeypatch):
    def boom(run_id, **kw):
        raise FileNotFoundError(f"No recorded run '{run_id}'")

    monkeypatch.setattr("meta_harness.webapp.routes_qa_flow.replay_qa_review", boom)

    response = client.post("/api/qa/reviews/missing/replay", json={})

    assert response.status_code == 404


def test_post_commit_review_persists_exact_content(tmp_path):
    test_client = _client_with_store(tmp_path)

    response = test_client.post(
        "/api/qa/reviews/commit",
        json={
            "ticket_id": "T1", "ticket_name": "Fix login bug",
            "observation": "Check login", "severity": "major", "project": "sigo-front",
        },
    )
    app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["route"] == "Fix login bug"
    assert body["observation"] == "Check login"
    assert body["severity"] == "major"


def test_post_commit_review_invalid_severity_returns_400(tmp_path):
    test_client = _client_with_store(tmp_path)

    response = test_client.post(
        "/api/qa/reviews/commit",
        json={
            "ticket_id": "T1", "ticket_name": "Fix login bug",
            "observation": "Check login", "severity": "urgentish", "project": "sigo-front",
        },
    )
    app.dependency_overrides.clear()

    assert response.status_code == 400
