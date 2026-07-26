import json

import pytest

from meta_harness.clickup_bridge import ClickUpTicketError, create_clickup_ticket


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
