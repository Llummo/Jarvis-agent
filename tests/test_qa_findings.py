import pytest

from meta_harness.clickup_bridge import ClickUpTicketError
from meta_harness.qa_findings import (
    QAFinding,
    QAFindingNotFoundError,
    QAFindingStore,
    close_qa_issue,
    list_qa_issues,
    report_qa_issue,
)


def _store(tmp_path) -> QAFindingStore:
    return QAFindingStore(tmp_path / "findings.db")


def test_report_qa_issue_minor_does_not_create_ticket(tmp_path, monkeypatch):
    def boom(**kwargs):
        raise AssertionError("should not be called for minor severity")

    monkeypatch.setattr("meta_harness.qa_findings.clickup_bridge.create_clickup_ticket", boom)

    finding = report_qa_issue(
        "sigo-front", "/checkout", "button misaligned", "minor", store=_store(tmp_path)
    )

    assert finding.status == "open"
    assert finding.clickup_task_id is None


def test_report_qa_issue_critical_creates_and_links_ticket(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "meta_harness.qa_findings.clickup_bridge.create_clickup_ticket", lambda **kw: "CU-1"
    )

    finding = report_qa_issue(
        "sigo-front", "/checkout", "500 on submit", "critical", store=_store(tmp_path)
    )

    assert finding.status == "acknowledged"
    assert finding.clickup_task_id == "CU-1"


def test_report_qa_issue_critical_ticket_failure_does_not_block_finding(tmp_path, monkeypatch):
    def fail(**kwargs):
        raise ClickUpTicketError("clickup down")

    monkeypatch.setattr("meta_harness.qa_findings.clickup_bridge.create_clickup_ticket", fail)

    finding = report_qa_issue(
        "sigo-front", "/checkout", "500 on submit", "critical", store=_store(tmp_path)
    )

    assert finding.status == "acknowledged"
    assert finding.clickup_task_id is None


def test_report_qa_issue_no_ticket_flag_skips_escalation(tmp_path, monkeypatch):
    def boom(**kwargs):
        raise AssertionError("should not be called when auto_escalate=False")

    monkeypatch.setattr("meta_harness.qa_findings.clickup_bridge.create_clickup_ticket", boom)

    finding = report_qa_issue(
        "sigo-front", "/checkout", "500 on submit", "critical",
        auto_escalate=False, store=_store(tmp_path),
    )

    assert finding.status == "acknowledged"
    assert finding.clickup_task_id is None


def test_report_qa_issue_rejects_invalid_severity(tmp_path):
    with pytest.raises(ValueError, match="severity"):
        report_qa_issue("p", "/r", "obs", "blocker", store=_store(tmp_path))


def test_list_qa_issues_filters_by_project_severity_status(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "meta_harness.qa_findings.clickup_bridge.create_clickup_ticket", lambda **kw: "CU-1"
    )
    store = _store(tmp_path)
    report_qa_issue("sigo-front", "/a", "obs a", "minor", store=store)
    report_qa_issue("sigo-front", "/b", "obs b", "critical", store=store)
    report_qa_issue("other-project", "/c", "obs c", "major", store=store)

    assert [f.route for f in list_qa_issues(project="sigo-front", store=store)] == ["/a", "/b"]
    assert [f.route for f in list_qa_issues(severity="critical", store=store)] == ["/b"]
    assert [f.route for f in list_qa_issues(status="open", store=store)] == ["/a", "/c"]


def test_close_qa_issue_sets_status_and_note(tmp_path):
    store = _store(tmp_path)
    finding = report_qa_issue("p", "/r", "obs", "minor", store=store)

    closed = close_qa_issue(finding.id, "fixed the null check", store=store)

    assert closed.status == "closed"
    assert closed.correction_note == "fixed the null check"


def test_close_qa_issue_missing_id_raises(tmp_path):
    with pytest.raises(QAFindingNotFoundError):
        close_qa_issue(999, "fixed", store=_store(tmp_path))


def test_close_qa_issue_already_closed_raises(tmp_path):
    store = _store(tmp_path)
    finding = report_qa_issue("p", "/r", "obs", "minor", store=store)
    close_qa_issue(finding.id, "fixed", store=store)

    with pytest.raises(ValueError, match="already closed"):
        close_qa_issue(finding.id, "fixed again", store=store)


def test_qa_finding_store_persists_across_instances(tmp_path):
    path = tmp_path / "findings.db"
    QAFindingStore(path).create(
        QAFinding(id=None, project="p", route="/r", observation="o", severity="minor")
    )

    reopened = QAFindingStore(path).list()

    assert len(reopened) == 1
    assert reopened[0].project == "p"


def test_report_qa_issue_saves_route_check_evidence(tmp_path):
    finding = report_qa_issue(
        "sigo-front", "Fix login bug", "Check login", "major", store=_store(tmp_path),
        checked_route="/login", status_code=500, http_error="HTTP 500: Internal Server Error",
        screenshot_path="qa/screenshots/shot.png",
    )

    assert finding.checked_route == "/login"
    assert finding.status_code == 500
    assert finding.http_error == "HTTP 500: Internal Server Error"
    assert finding.screenshot_path == "qa/screenshots/shot.png"


def test_existing_database_migrates_to_include_new_columns(tmp_path):
    # Simulate a database created before checked_route/status_code/http_error
    # existed, to prove the ALTER TABLE migration in QAFindingStore.__init__
    # doesn't break (or lose data from) an already-populated older database.
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE qa_findings (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            project          TEXT NOT NULL,
            route            TEXT NOT NULL,
            observation      TEXT NOT NULL,
            severity         TEXT NOT NULL,
            status           TEXT NOT NULL DEFAULT 'open',
            screenshot_path  TEXT,
            correction_note  TEXT,
            clickup_task_id  TEXT,
            created_at       TEXT NOT NULL,
            updated_at       TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO qa_findings (project, route, observation, severity, status, created_at, updated_at) "
        "VALUES ('p', '/r', 'old finding', 'minor', 'open', 't', 't')"
    )
    conn.commit()
    conn.close()

    store = QAFindingStore(path)
    findings = store.list()

    assert len(findings) == 1
    assert findings[0].observation == "old finding"
    assert findings[0].checked_route is None
    assert findings[0].status_code is None
