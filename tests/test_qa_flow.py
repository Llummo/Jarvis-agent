import json

import pytest

from meta_harness.qa_findings import QAFindingStore
from meta_harness.qa_flow import (
    ClaudeNotFoundError,
    QATicketReview,
    ReviewGenerationError,
    ReviewParseError,
    analyze_ticket,
    persist_review,
    replay_qa_review,
    review_and_record,
    review_qa_ticket,
)
from meta_harness.run_archive import RunArchive


class Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


VALID_REVIEW = json.dumps({"observation": "Check the login flow works", "severity": "major"})


# ---------------------------------------------------------------------------
# analyze_ticket — harnessed claude -p call
# ---------------------------------------------------------------------------


def test_analyze_ticket_happy_path(monkeypatch):
    monkeypatch.setattr("meta_harness.qa_flow.shutil.which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr("meta_harness.qa_flow.subprocess.run", lambda *a, **k: Result(stdout=VALID_REVIEW))

    review = analyze_ticket("T1", "Fix login bug", "Users can't log in", timeout_s=5)

    assert review.ticket_id == "T1"
    assert review.observation == "Check the login flow works"
    assert review.severity == "major"


def test_analyze_ticket_raises_when_claude_not_found(monkeypatch):
    monkeypatch.delenv("META_HARNESS_CLAUDE_PATH", raising=False)
    monkeypatch.setattr("meta_harness.qa_flow.shutil.which", lambda name: None)

    with pytest.raises(ClaudeNotFoundError):
        analyze_ticket("T1", "title", "desc")


def test_analyze_ticket_retries_and_recovers_from_bad_json(monkeypatch):
    calls = []

    def fake_run(command, capture_output, text, timeout):
        calls.append(command)
        if len(calls) == 1:
            return Result(stdout="not json")
        return Result(stdout=VALID_REVIEW)

    monkeypatch.setattr("meta_harness.qa_flow.shutil.which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr("meta_harness.qa_flow.subprocess.run", fake_run)

    review = analyze_ticket("T1", "title", "desc", timeout_s=5)

    assert len(calls) == 2
    assert review.severity == "major"
    assert "previous response was invalid" in calls[1][2]


def test_analyze_ticket_gives_up_after_max_attempts(monkeypatch):
    calls = []
    monkeypatch.setattr("meta_harness.qa_flow.shutil.which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(
        "meta_harness.qa_flow.subprocess.run",
        lambda *a, **k: (calls.append(1), Result(stdout="still bad"))[1],
    )

    with pytest.raises(ReviewParseError):
        analyze_ticket("T1", "title", "desc", timeout_s=5, max_attempts=3)

    assert len(calls) == 3


def test_analyze_ticket_raises_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr("meta_harness.qa_flow.shutil.which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(
        "meta_harness.qa_flow.subprocess.run",
        lambda *a, **k: Result(returncode=1, stderr="boom"),
    )

    with pytest.raises(ReviewGenerationError, match="boom"):
        analyze_ticket("T1", "title", "desc", timeout_s=5)


def test_analyze_ticket_reports_steps_via_on_step(monkeypatch):
    calls = []

    def fake_run(command, capture_output, text, timeout):
        if len(calls) == 0:
            calls.append(1)
            return Result(stdout="not json")
        return Result(stdout=VALID_REVIEW)

    monkeypatch.setattr("meta_harness.qa_flow.shutil.which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr("meta_harness.qa_flow.subprocess.run", fake_run)

    steps = []
    analyze_ticket("T1", "title", "desc", timeout_s=5, on_step=steps.append)

    assert steps[0] == "Claude is analyzing the ticket…"
    assert "attempt 2/3" in steps[1]
    assert "did not return valid JSON" in steps[1]


def test_analyze_ticket_rejects_invalid_severity(monkeypatch):
    monkeypatch.setattr("meta_harness.qa_flow.shutil.which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(
        "meta_harness.qa_flow.subprocess.run",
        lambda *a, **k: Result(stdout=json.dumps({"observation": "x", "severity": "urgentish"})),
    )

    with pytest.raises(ReviewParseError, match="invalid severity"):
        analyze_ticket("T1", "title", "desc", timeout_s=5, max_attempts=1)


# ---------------------------------------------------------------------------
# review_qa_ticket — dry-run vs persist
# ---------------------------------------------------------------------------


def _mock_ticket_fetch(monkeypatch, name="Fix login bug", description="Users can't log in"):
    monkeypatch.setattr(
        "meta_harness.qa_flow.get_clickup_task",
        lambda ticket_id, **kw: {"id": ticket_id, "name": name, "text_content": description},
    )


def _mock_analysis(monkeypatch, severity="major"):
    monkeypatch.setattr(
        "meta_harness.qa_flow.shutil.which", lambda name: "/usr/bin/claude"
    )
    monkeypatch.setattr(
        "meta_harness.qa_flow.subprocess.run",
        lambda *a, **k: Result(stdout=json.dumps({"observation": "Check login", "severity": severity})),
    )


def test_review_qa_ticket_dry_run_does_not_persist(tmp_path, monkeypatch):
    _mock_ticket_fetch(monkeypatch)
    _mock_analysis(monkeypatch)

    def boom(*a, **kw):
        raise AssertionError("should not report a finding on dry-run")

    monkeypatch.setattr("meta_harness.qa_flow.report_qa_issue", boom)

    review, finding = review_qa_ticket("T1", project="sigo-front", store=QAFindingStore(tmp_path / "f.db"))

    assert review.ticket_name == "Fix login bug"
    assert finding is None


def test_review_qa_ticket_persist_creates_finding(tmp_path, monkeypatch):
    _mock_ticket_fetch(monkeypatch)
    _mock_analysis(monkeypatch, severity="critical")

    store = QAFindingStore(tmp_path / "f.db")
    captured = {}

    def fake_report(project, route, observation, severity, **kw):
        captured.update(project=project, route=route, observation=observation, severity=severity)
        from meta_harness.qa_findings import report_qa_issue as real_report
        return real_report(project, route, observation, severity, store=store, auto_escalate=False)

    monkeypatch.setattr("meta_harness.qa_flow.report_qa_issue", fake_report)

    review, finding = review_qa_ticket("T1", project="sigo-front", persist=True, store=store)

    assert finding is not None
    assert finding.severity == "critical"
    assert captured["route"] == "Fix login bug"


def test_review_qa_ticket_reports_fetch_and_persist_steps(tmp_path, monkeypatch):
    _mock_ticket_fetch(monkeypatch)
    _mock_analysis(monkeypatch)
    store = QAFindingStore(tmp_path / "f.db")

    steps = []
    review_qa_ticket("T1", project="sigo-front", persist=True, store=store, on_step=steps.append)

    assert steps[0] == "Fetching ticket T1 from ClickUp…"
    assert "Claude is analyzing the ticket…" in steps
    assert steps[-1] == "Saving finding to the database…"


# ---------------------------------------------------------------------------
# review_and_record / replay_qa_review
# ---------------------------------------------------------------------------


def test_review_and_record_persists_run_archive(tmp_path, monkeypatch):
    _mock_ticket_fetch(monkeypatch)
    _mock_analysis(monkeypatch)
    archive = RunArchive("qa_ticket_review", directory=tmp_path)

    review, finding, record = review_and_record(
        "T1", project="sigo-front", archive=archive, store=QAFindingStore(tmp_path / "f.db")
    )

    assert finding is None  # dry-run by default
    assert record.subject_id == "T1"
    payload = json.loads(record.steps[0].stdout)
    assert payload["project"] == "sigo-front"
    assert payload["persisted"] is False
    assert len(archive.load()) == 1


def test_replay_qa_review_reuses_same_ticket_and_project(tmp_path, monkeypatch):
    _mock_ticket_fetch(monkeypatch)
    _mock_analysis(monkeypatch)
    archive = RunArchive("qa_ticket_review", directory=tmp_path)
    store = QAFindingStore(tmp_path / "f.db")

    _, _, original_record = review_and_record("T1", project="sigo-front", archive=archive, store=store)

    original, review, finding, new_record = replay_qa_review(original_record.run_id, archive=archive, store=store)

    assert original.run_id == original_record.run_id
    assert review.ticket_id == "T1"
    assert finding is None  # replay is dry-run by default too
    assert new_record.run_id != original.run_id
    assert len(archive.load()) == 2


def test_replay_qa_review_can_persist(tmp_path, monkeypatch):
    _mock_ticket_fetch(monkeypatch)
    _mock_analysis(monkeypatch)
    archive = RunArchive("qa_ticket_review", directory=tmp_path)
    store = QAFindingStore(tmp_path / "f.db")

    def fake_report(project, route, observation, severity, **kw):
        from meta_harness.qa_findings import report_qa_issue as real_report
        return real_report(project, route, observation, severity, store=store, auto_escalate=False)

    monkeypatch.setattr("meta_harness.qa_flow.report_qa_issue", fake_report)

    _, _, original_record = review_and_record("T1", project="sigo-front", archive=archive, store=store)

    _, _, finding, _ = replay_qa_review(original_record.run_id, persist=True, archive=archive, store=store)

    assert finding is not None


def test_replay_qa_review_reports_steps_via_on_step(tmp_path, monkeypatch):
    _mock_ticket_fetch(monkeypatch)
    _mock_analysis(monkeypatch)
    archive = RunArchive("qa_ticket_review", directory=tmp_path)
    store = QAFindingStore(tmp_path / "f.db")
    _, _, original_record = review_and_record("T1", project="sigo-front", archive=archive, store=store)

    steps = []
    replay_qa_review(original_record.run_id, archive=archive, store=store, on_step=steps.append)

    assert steps[0] == f"Loading recorded run {original_record.run_id}…"
    assert "Fetching ticket T1 from ClickUp…" in steps


def test_replay_qa_review_missing_run_id_raises(tmp_path):
    archive = RunArchive("qa_ticket_review", directory=tmp_path)

    with pytest.raises(FileNotFoundError):
        replay_qa_review("missing", archive=archive)


# ---------------------------------------------------------------------------
# persist_review — commit an already-computed review without re-analyzing
# ---------------------------------------------------------------------------


def test_persist_review_saves_exact_reviewed_content(tmp_path):
    store = QAFindingStore(tmp_path / "f.db")
    review = QATicketReview(ticket_id="T1", ticket_name="Fix login bug", observation="Check login", severity="major")

    finding = persist_review(review, project="sigo-front", store=store)

    assert finding.project == "sigo-front"
    assert finding.route == "Fix login bug"
    assert finding.observation == "Check login"
    assert finding.severity == "major"


def test_persist_review_does_not_call_claude(tmp_path, monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("persist_review must not re-analyze")

    monkeypatch.setattr("meta_harness.qa_flow.subprocess.run", boom)
    store = QAFindingStore(tmp_path / "f.db")
    review = QATicketReview(ticket_id="T1", ticket_name="Fix login bug", observation="Check login", severity="minor")

    persist_review(review, project="sigo-front", store=store)
