import json

import pytest

from meta_harness.clickup_bridge import (
    ClickUpReadError,
    ClickUpTicketError,
    create_clickup_ticket,
    list_clickup_lists,
    list_clickup_spaces,
    list_clickup_teams,
)


class Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_create_clickup_ticket_parses_task_id(tmp_path, monkeypatch):
    calls = []

    def fake_run(command, cwd, capture_output, text):
        calls.append(list(command))
        return Result(stdout=json.dumps({"id": "CU-123"}))

    monkeypatch.setattr("meta_harness.clickup_bridge.subprocess.run", fake_run)

    task_id = create_clickup_ticket("name", "desc", list_id="901", project_path=tmp_path)

    assert task_id == "CU-123"
    assert "--list-id" in calls[0]
    assert "901" in calls[0]
    assert calls[0][0] == str(tmp_path / ".venv" / "bin" / "harness")


def test_create_clickup_ticket_omits_list_id_when_not_given(tmp_path, monkeypatch):
    calls = []

    def fake_run(command, cwd, capture_output, text):
        calls.append(list(command))
        return Result(stdout=json.dumps({"id": "CU-1"}))

    monkeypatch.setattr("meta_harness.clickup_bridge.subprocess.run", fake_run)

    create_clickup_ticket("name", "desc", project_path=tmp_path)

    assert "--list-id" not in calls[0]


def test_create_clickup_ticket_raises_on_nonzero_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "meta_harness.clickup_bridge.subprocess.run",
        lambda *a, **k: Result(returncode=1, stderr="boom"),
    )

    with pytest.raises(ClickUpTicketError, match="boom"):
        create_clickup_ticket("name", "desc", project_path=tmp_path)


def test_create_clickup_ticket_raises_on_invalid_json(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "meta_harness.clickup_bridge.subprocess.run",
        lambda *a, **k: Result(stdout="not json"),
    )

    with pytest.raises(ClickUpTicketError, match="Could not parse"):
        create_clickup_ticket("name", "desc", project_path=tmp_path)


def test_create_clickup_ticket_raises_when_id_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "meta_harness.clickup_bridge.subprocess.run",
        lambda *a, **k: Result(stdout=json.dumps({"name": "no id here"})),
    )

    with pytest.raises(ClickUpTicketError, match="missing 'id'"):
        create_clickup_ticket("name", "desc", project_path=tmp_path)


def test_list_clickup_teams_builds_correct_command(tmp_path, monkeypatch):
    calls = []

    def fake_run(command, cwd, capture_output, text):
        calls.append(list(command))
        return Result(stdout=json.dumps([{"id": "T1", "name": "Team 1"}]))

    monkeypatch.setattr("meta_harness.clickup_bridge.subprocess.run", fake_run)

    teams = list_clickup_teams(project_path=tmp_path)

    assert teams == [{"id": "T1", "name": "Team 1"}]
    assert calls[0] == [str(tmp_path / ".venv" / "bin" / "harness"), "clickup", "teams"]


def test_list_clickup_spaces_builds_correct_command(tmp_path, monkeypatch):
    calls = []

    def fake_run(command, cwd, capture_output, text):
        calls.append(list(command))
        return Result(stdout=json.dumps([{"id": "S1", "name": "Space 1"}]))

    monkeypatch.setattr("meta_harness.clickup_bridge.subprocess.run", fake_run)

    spaces = list_clickup_spaces("T1", project_path=tmp_path)

    assert spaces == [{"id": "S1", "name": "Space 1"}]
    assert calls[0] == [
        str(tmp_path / ".venv" / "bin" / "harness"), "clickup", "spaces", "--team-id", "T1",
    ]


def test_list_clickup_lists_builds_correct_command(tmp_path, monkeypatch):
    calls = []

    def fake_run(command, cwd, capture_output, text):
        calls.append(list(command))
        return Result(stdout=json.dumps([{"id": "L1", "name": "List 1"}]))

    monkeypatch.setattr("meta_harness.clickup_bridge.subprocess.run", fake_run)

    lists = list_clickup_lists("S1", project_path=tmp_path)

    assert lists == [{"id": "L1", "name": "List 1"}]
    assert calls[0] == [
        str(tmp_path / ".venv" / "bin" / "harness"), "clickup", "lists", "--space-id", "S1",
    ]


def test_clickup_read_raises_on_nonzero_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "meta_harness.clickup_bridge.subprocess.run",
        lambda *a, **k: Result(returncode=1, stderr="upstream boom"),
    )

    with pytest.raises(ClickUpReadError, match="upstream boom"):
        list_clickup_teams(project_path=tmp_path)


def test_clickup_read_raises_on_invalid_json(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "meta_harness.clickup_bridge.subprocess.run",
        lambda *a, **k: Result(stdout="not json"),
    )

    with pytest.raises(ClickUpReadError, match="Could not parse"):
        list_clickup_teams(project_path=tmp_path)


def test_clickup_read_raises_when_payload_is_not_a_list(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "meta_harness.clickup_bridge.subprocess.run",
        lambda *a, **k: Result(stdout=json.dumps({"not": "a list"})),
    )

    with pytest.raises(ClickUpReadError, match="Expected a JSON array"):
        list_clickup_teams(project_path=tmp_path)
