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
