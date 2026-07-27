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


def test_post_review_passes_pass_and_fail_status_through(monkeypatch):
    captured = {}

    def fake_review_and_record(ticket_id, **kw):
        captured.update(kw)
        return (
            _review(), None,
            RunRecord(run_id="run1", agent="qa_ticket_review", subject_id=ticket_id, started_at="t", ok=True),
        )

    monkeypatch.setattr("meta_harness.webapp.routes_qa_flow.review_and_record", fake_review_and_record)

    client.post(
        "/api/qa/reviews",
        json={"ticket_id": "T1", "project": "sigo-front", "persist": True, "pass_status": "done", "fail_status": "blocked"},
    )

    assert captured["pass_status"] == "done"
    assert captured["fail_status"] == "blocked"


def test_post_commit_review_moves_status_on_pass(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "meta_harness.qa_flow.update_clickup_task_status",
        lambda task_id, status, **kw: calls.append((task_id, status)),
    )
    test_client = _client_with_store(tmp_path)

    response = test_client.post(
        "/api/qa/reviews/commit",
        json={
            "ticket_id": "T1", "ticket_name": "Fix login bug",
            "observation": "Check login", "severity": "minor", "project": "sigo-front",
            "pass_status": "done", "fail_status": "blocked",
        },
    )
    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert calls == [("T1", "done")]


def test_post_commit_review_saves_route_check_evidence(tmp_path):
    test_client = _client_with_store(tmp_path)

    response = test_client.post(
        "/api/qa/reviews/commit",
        json={
            "ticket_id": "T1", "ticket_name": "Fix login bug",
            "observation": "Check login", "severity": "critical", "project": "sigo-front",
            "route": "/login", "status_code": 500, "http_error": "HTTP 500: Internal Server Error",
            "screenshot_path": "qa/screenshots/shot.png",
        },
    )
    app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["checked_route"] == "/login"
    assert body["status_code"] == 500
    assert body["http_error"] == "HTTP 500: Internal Server Error"
    assert body["screenshot_path"] == "qa/screenshots/shot.png"


def test_post_bulk_review_returns_per_ticket_results(monkeypatch):
    def fake_bulk(ticket_ids, **kw):
        return [(tid, _review(), None) for tid in ticket_ids]

    monkeypatch.setattr("meta_harness.webapp.routes_qa_flow.review_tickets_bulk", fake_bulk)

    response = client.post(
        "/api/qa/reviews/bulk", json={"ticket_ids": ["T1", "T2"], "project": "sigo-front"}
    )

    assert response.status_code == 200
    results = response.json()["results"]
    assert [r["ticket_id"] for r in results] == ["T1", "T2"]
    assert all(r["review"]["severity"] == "major" for r in results)


def test_post_bulk_review_reports_per_ticket_errors(monkeypatch):
    monkeypatch.setattr(
        "meta_harness.webapp.routes_qa_flow.review_tickets_bulk",
        lambda ticket_ids, **kw: [("T1", None, "ticket not found")],
    )

    response = client.post("/api/qa/reviews/bulk", json={"ticket_ids": ["T1"], "project": "sigo-front"})

    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["review"] is None
    assert result["error"] == "ticket not found"


def test_post_bulk_commit_persists_each_item_and_isolates_failures(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "meta_harness.qa_flow.update_clickup_task_status",
        lambda task_id, status, **kw: calls.append((task_id, status)),
    )
    test_client = _client_with_store(tmp_path)

    response = test_client.post(
        "/api/qa/reviews/bulk/commit",
        json={
            "items": [
                {
                    "ticket_id": "T1", "ticket_name": "Fix login bug", "observation": "Check login",
                    "severity": "minor", "project": "sigo-front", "pass_status": "done", "fail_status": "blocked",
                },
                {
                    "ticket_id": "T2", "ticket_name": "Fix logout bug", "observation": "Check logout",
                    "severity": "urgentish", "project": "sigo-front",
                },
            ]
        },
    )
    app.dependency_overrides.clear()

    assert response.status_code == 200
    results = response.json()["results"]
    assert results[0]["ticket_id"] == "T1"
    assert results[0]["finding"]["route"] == "Fix login bug"
    assert results[0]["error"] is None
    assert results[1]["ticket_id"] == "T2"
    assert results[1]["finding"] is None
    assert results[1]["error"] is not None
    assert calls == [("T1", "done")]


def test_post_bulk_commit_saves_route_check_evidence(tmp_path):
    test_client = _client_with_store(tmp_path)

    response = test_client.post(
        "/api/qa/reviews/bulk/commit",
        json={
            "items": [
                {
                    "ticket_id": "T1", "ticket_name": "Fix login bug", "observation": "Check login",
                    "severity": "critical", "project": "sigo-front",
                    "route": "/login", "status_code": 500, "http_error": "HTTP 500: Internal Server Error",
                    "screenshot_path": "qa/screenshots/shot.png",
                },
            ]
        },
    )
    app.dependency_overrides.clear()

    assert response.status_code == 200
    finding = response.json()["results"][0]["finding"]
    assert finding["checked_route"] == "/login"
    assert finding["status_code"] == 500
    assert finding["http_error"] == "HTTP 500: Internal Server Error"
    assert finding["screenshot_path"] == "qa/screenshots/shot.png"


def test_post_review_passes_tracker_and_linear_team_id(monkeypatch):
    captured = {}

    def fake_review_and_record(ticket_id, **kw):
        captured.update(kw)
        return (
            _review(), None,
            RunRecord(run_id="run1", agent="qa_ticket_review", subject_id=ticket_id, started_at="t", ok=True),
        )

    monkeypatch.setattr("meta_harness.webapp.routes_qa_flow.review_and_record", fake_review_and_record)

    response = client.post(
        "/api/qa/reviews",
        json={"ticket_id": "I1", "project": "sigo", "tracker": "linear", "linear_team_id": "team-1"},
    )

    assert response.status_code == 200
    assert captured["tracker"] == "linear"
    assert captured["linear_team_id"] == "team-1"


def test_post_bulk_review_passes_tracker_and_linear_team_id(monkeypatch):
    captured = {}

    def fake_bulk(ticket_ids, **kw):
        captured.update(kw)
        return []

    monkeypatch.setattr("meta_harness.webapp.routes_qa_flow.review_tickets_bulk", fake_bulk)

    response = client.post(
        "/api/qa/reviews/bulk",
        json={"ticket_ids": ["I1"], "project": "sigo", "tracker": "linear", "linear_team_id": "team-1"},
    )

    assert response.status_code == 200
    assert captured["tracker"] == "linear"
    assert captured["linear_team_id"] == "team-1"
