from fastapi.testclient import TestClient

from meta_harness.qa_findings import QAFindingStore
from meta_harness.webapp.app import app
from meta_harness.webapp.deps import get_qa_store


def _client_with_store(tmp_path) -> TestClient:
    store = QAFindingStore(tmp_path / "findings.db")
    app.dependency_overrides[get_qa_store] = lambda: store
    return TestClient(app)


def test_get_root_serves_static_shell(tmp_path):
    client = _client_with_store(tmp_path)
    response = client.get("/")
    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert "<html" in response.text.lower()


def test_report_and_list_findings_round_trip(tmp_path):
    client = _client_with_store(tmp_path)

    post_response = client.post(
        "/api/qa/findings",
        json={"project": "sigo-front", "route": "/checkout", "observation": "misaligned", "severity": "minor"},
    )
    assert post_response.status_code == 200
    assert post_response.json()["status"] == "open"

    list_response = client.get("/api/qa/findings")
    app.dependency_overrides.clear()

    assert list_response.status_code == 200
    body = list_response.json()
    assert body["counts"]["total"] == 1
    assert body["counts"]["minor"] == 1
    assert body["findings"][0]["route"] == "/checkout"


def test_report_finding_invalid_severity_returns_400(tmp_path):
    client = _client_with_store(tmp_path)

    response = client.post(
        "/api/qa/findings",
        json={"project": "p", "route": "/r", "observation": "o", "severity": "blocker"},
    )
    app.dependency_overrides.clear()

    assert response.status_code == 400


def test_close_finding_not_found_returns_404(tmp_path):
    client = _client_with_store(tmp_path)

    response = client.post("/api/qa/findings/999/close", json={"correction_note": "fixed"})
    app.dependency_overrides.clear()

    assert response.status_code == 404


def test_close_already_closed_finding_returns_409(tmp_path):
    client = _client_with_store(tmp_path)

    post_response = client.post(
        "/api/qa/findings",
        json={"project": "p", "route": "/r", "observation": "o", "severity": "minor"},
    )
    finding_id = post_response.json()["id"]
    client.post(f"/api/qa/findings/{finding_id}/close", json={"correction_note": "fixed"})

    second_close = client.post(f"/api/qa/findings/{finding_id}/close", json={"correction_note": "again"})
    app.dependency_overrides.clear()

    assert second_close.status_code == 409


def test_findings_filter_by_severity(tmp_path):
    client = _client_with_store(tmp_path)
    client.post(
        "/api/qa/findings",
        json={"project": "p", "route": "/a", "observation": "o", "severity": "minor"},
    )
    client.post(
        "/api/qa/findings",
        json={"project": "p", "route": "/b", "observation": "o", "severity": "major"},
    )

    response = client.get("/api/qa/findings", params={"severity": "major"})
    app.dependency_overrides.clear()

    body = response.json()
    assert len(body["findings"]) == 1
    assert body["findings"][0]["route"] == "/b"
    assert body["counts"]["total"] == 2  # counts reflect the full unfiltered set
