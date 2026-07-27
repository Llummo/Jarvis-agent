import json

import pytest

from meta_harness.linear_bridge import (
    LinearIssueError,
    LinearReadError,
    create_linear_issue,
    get_linear_issue,
    list_linear_issues,
    list_linear_members,
    list_linear_states,
    list_linear_teams,
    update_linear_issue_state,
)


class Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _fake_run(monkeypatch, calls, stdout):
    def fake_run(command, cwd, capture_output, text):
        calls.append(list(command))
        return Result(stdout=stdout)

    monkeypatch.setattr("meta_harness.linear_bridge.subprocess.run", fake_run)


def test_list_linear_teams(tmp_path, monkeypatch):
    calls = []
    _fake_run(monkeypatch, calls, json.dumps([{"id": "t1", "name": "Sigo"}]))

    teams = list_linear_teams(project_path=tmp_path)

    assert teams[0]["name"] == "Sigo"
    assert "linear" in calls[0] and "teams" in calls[0]


def test_list_linear_states_passes_team_id(tmp_path, monkeypatch):
    calls = []
    _fake_run(monkeypatch, calls, json.dumps([{"id": "s1", "name": "Todo"}]))

    list_linear_states("t1", project_path=tmp_path)

    assert "--team-id" in calls[0] and "t1" in calls[0]


def test_list_linear_members(tmp_path, monkeypatch):
    calls = []
    _fake_run(monkeypatch, calls, json.dumps([{"id": "u1", "email": "a@x.com"}]))

    members = list_linear_members("t1", project_path=tmp_path)

    assert members[0]["email"] == "a@x.com"


def test_list_linear_issues_passes_first(tmp_path, monkeypatch):
    calls = []
    _fake_run(monkeypatch, calls, json.dumps([{"id": "i1"}]))

    list_linear_issues("t1", first=10, project_path=tmp_path)

    assert "--first" in calls[0] and "10" in calls[0]


def test_get_linear_issue(tmp_path, monkeypatch):
    calls = []
    _fake_run(monkeypatch, calls, json.dumps({"id": "i1", "identifier": "SIG-1"}))

    issue = get_linear_issue("i1", project_path=tmp_path)

    assert issue["identifier"] == "SIG-1"


def test_get_linear_issue_non_object_raises(tmp_path, monkeypatch):
    calls = []
    _fake_run(monkeypatch, calls, json.dumps(["not", "an", "object"]))

    with pytest.raises(LinearReadError, match="Expected a JSON object"):
        get_linear_issue("i1", project_path=tmp_path)


def test_create_linear_issue_returns_id(tmp_path, monkeypatch):
    calls = []
    _fake_run(monkeypatch, calls, json.dumps({"id": "i1", "identifier": "SIG-1"}))

    issue_id = create_linear_issue("t1", "Title", "Desc", project_path=tmp_path)

    assert issue_id == "i1"
    assert "create-issue" in calls[0]


def test_create_linear_issue_passes_optional_fields(tmp_path, monkeypatch):
    calls = []
    _fake_run(monkeypatch, calls, json.dumps({"id": "i1"}))

    create_linear_issue(
        "t1", "Title", "Desc", priority="urgent", assignee_id="u1",
        due_date="2026-08-24", project_path=tmp_path,
    )

    assert "--priority" in calls[0] and "urgent" in calls[0]
    assert "--assignee-id" in calls[0] and "u1" in calls[0]
    assert "--due-date" in calls[0] and "2026-08-24" in calls[0]


def test_create_linear_issue_omits_optional_fields_when_absent(tmp_path, monkeypatch):
    calls = []
    _fake_run(monkeypatch, calls, json.dumps({"id": "i1"}))

    create_linear_issue("t1", "Title", "Desc", project_path=tmp_path)

    assert "--priority" not in calls[0]
    assert "--assignee-id" not in calls[0]
    assert "--due-date" not in calls[0]


def test_create_linear_issue_rejects_invalid_priority(tmp_path):
    with pytest.raises(ValueError, match="priority must be one of"):
        create_linear_issue("t1", "Title", "Desc", priority="urgentish", project_path=tmp_path)


def test_create_linear_issue_raises_on_nonzero_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "meta_harness.linear_bridge.subprocess.run",
        lambda *a, **k: Result(returncode=1, stderr="boom"),
    )

    with pytest.raises(LinearIssueError, match="boom"):
        create_linear_issue("t1", "Title", "Desc", project_path=tmp_path)


def test_create_linear_issue_raises_when_id_missing(tmp_path, monkeypatch):
    calls = []
    _fake_run(monkeypatch, calls, json.dumps({"identifier": "SIG-1"}))

    with pytest.raises(LinearIssueError, match="missing 'id'"):
        create_linear_issue("t1", "Title", "Desc", project_path=tmp_path)


def test_update_linear_issue_state(tmp_path, monkeypatch):
    calls = []
    _fake_run(monkeypatch, calls, json.dumps({"id": "i1", "state": {"name": "Done"}}))

    issue = update_linear_issue_state("i1", "s2", project_path=tmp_path)

    assert issue["state"]["name"] == "Done"
    assert "set-state" in calls[0]
    assert "--state-id" in calls[0] and "s2" in calls[0]


def test_update_linear_issue_state_raises_on_nonzero_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "meta_harness.linear_bridge.subprocess.run",
        lambda *a, **k: Result(returncode=1, stderr="state not found"),
    )

    with pytest.raises(LinearIssueError, match="state not found"):
        update_linear_issue_state("i1", "bad", project_path=tmp_path)


def test_read_error_on_invalid_json(tmp_path, monkeypatch):
    calls = []
    _fake_run(monkeypatch, calls, "not json")

    with pytest.raises(LinearReadError, match="Could not parse"):
        list_linear_teams(project_path=tmp_path)
