import json

import pytest

from meta_harness.clickup_bridge import (
    ClickUpReadError,
    ClickUpTicketError,
    create_clickup_ticket,
    get_clickup_task,
    list_clickup_folders,
    list_clickup_lists,
    list_clickup_spaces,
    list_clickup_tasks,
    list_clickup_teams,
    update_clickup_task_status,
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


def test_create_clickup_ticket_passes_assignees(tmp_path, monkeypatch):
    calls = []

    def fake_run(command, cwd, capture_output, text):
        calls.append(list(command))
        return Result(stdout=json.dumps({"id": "CU-1"}))

    monkeypatch.setattr("meta_harness.clickup_bridge.subprocess.run", fake_run)

    create_clickup_ticket("name", "desc", assignees=[123, 456], project_path=tmp_path)

    assert "--assignees" in calls[0]
    assert "123,456" in calls[0]


def test_create_clickup_ticket_omits_assignees_when_not_given(tmp_path, monkeypatch):
    calls = []

    def fake_run(command, cwd, capture_output, text):
        calls.append(list(command))
        return Result(stdout=json.dumps({"id": "CU-1"}))

    monkeypatch.setattr("meta_harness.clickup_bridge.subprocess.run", fake_run)

    create_clickup_ticket("name", "desc", project_path=tmp_path)

    assert "--assignees" not in calls[0]


def test_create_clickup_ticket_passes_due_date(tmp_path, monkeypatch):
    calls = []

    def fake_run(command, cwd, capture_output, text):
        calls.append(list(command))
        return Result(stdout=json.dumps({"id": "CU-1"}))

    monkeypatch.setattr("meta_harness.clickup_bridge.subprocess.run", fake_run)

    create_clickup_ticket("name", "desc", due_date_ms=1735689600000, project_path=tmp_path)

    assert "--due-date" in calls[0]
    assert "1735689600000" in calls[0]


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


def test_update_clickup_task_status_calls_set_status(tmp_path, monkeypatch):
    calls = []

    def fake_run(command, cwd, capture_output, text):
        calls.append(list(command))
        return Result(stdout=json.dumps({"id": "CU-1", "status": {"status": "done"}}))

    monkeypatch.setattr("meta_harness.clickup_bridge.subprocess.run", fake_run)

    payload = update_clickup_task_status("CU-1", "done", project_path=tmp_path)

    assert payload["status"]["status"] == "done"
    assert "set-status" in calls[0]
    assert "--task-id" in calls[0] and "CU-1" in calls[0]
    assert "--status" in calls[0] and "done" in calls[0]


def test_update_clickup_task_status_raises_on_nonzero_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "meta_harness.clickup_bridge.subprocess.run",
        lambda *a, **k: Result(returncode=1, stderr="Status not found"),
    )

    with pytest.raises(ClickUpTicketError, match="Status not found"):
        update_clickup_task_status("CU-1", "not-a-real-status", project_path=tmp_path)


def test_update_clickup_task_status_raises_on_invalid_json(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "meta_harness.clickup_bridge.subprocess.run",
        lambda *a, **k: Result(stdout="not json"),
    )

    with pytest.raises(ClickUpTicketError, match="Could not parse"):
        update_clickup_task_status("CU-1", "done", project_path=tmp_path)


def test_create_clickup_ticket_passes_priority(tmp_path, monkeypatch):
    calls = []

    def fake_run(command, cwd, capture_output, text):
        calls.append(list(command))
        return Result(stdout=json.dumps({"id": "CU-1"}))

    monkeypatch.setattr("meta_harness.clickup_bridge.subprocess.run", fake_run)

    create_clickup_ticket("name", "desc", priority="urgent", project_path=tmp_path)

    assert "--priority" in calls[0]
    assert "urgent" in calls[0]


def test_create_clickup_ticket_omits_priority_when_not_given(tmp_path, monkeypatch):
    calls = []

    def fake_run(command, cwd, capture_output, text):
        calls.append(list(command))
        return Result(stdout=json.dumps({"id": "CU-1"}))

    monkeypatch.setattr("meta_harness.clickup_bridge.subprocess.run", fake_run)

    create_clickup_ticket("name", "desc", project_path=tmp_path)

    assert "--priority" not in calls[0]


def test_create_clickup_ticket_rejects_invalid_priority(tmp_path):
    with pytest.raises(ValueError, match="priority must be one of"):
        create_clickup_ticket("name", "desc", priority="urgentish", project_path=tmp_path)


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


def test_list_clickup_lists_by_folder_id(tmp_path, monkeypatch):
    calls = []

    def fake_run(command, cwd, capture_output, text):
        calls.append(list(command))
        return Result(stdout=json.dumps([{"id": "L2", "name": "Sprint backlog"}]))

    monkeypatch.setattr("meta_harness.clickup_bridge.subprocess.run", fake_run)

    lists = list_clickup_lists(folder_id="F1", project_path=tmp_path)

    assert lists == [{"id": "L2", "name": "Sprint backlog"}]
    assert calls[0] == [
        str(tmp_path / ".venv" / "bin" / "harness"), "clickup", "lists", "--folder-id", "F1",
    ]


def test_list_clickup_lists_requires_space_or_folder(tmp_path):
    with pytest.raises(ValueError, match="Provide either space_id or folder_id"):
        list_clickup_lists(project_path=tmp_path)


def test_list_clickup_folders_builds_correct_command(tmp_path, monkeypatch):
    calls = []

    def fake_run(command, cwd, capture_output, text):
        calls.append(list(command))
        return Result(stdout=json.dumps([{"id": "F1", "name": "Project-1"}]))

    monkeypatch.setattr("meta_harness.clickup_bridge.subprocess.run", fake_run)

    folders = list_clickup_folders("S1", project_path=tmp_path)

    assert folders == [{"id": "F1", "name": "Project-1"}]
    assert calls[0] == [
        str(tmp_path / ".venv" / "bin" / "harness"), "clickup", "folders", "--space-id", "S1",
    ]


def test_list_clickup_tasks_builds_correct_command(tmp_path, monkeypatch):
    calls = []

    def fake_run(command, cwd, capture_output, text):
        calls.append(list(command))
        return Result(stdout=json.dumps([{"id": "T1", "name": "Task 1"}]))

    monkeypatch.setattr("meta_harness.clickup_bridge.subprocess.run", fake_run)

    tasks = list_clickup_tasks("L1", project_path=tmp_path)

    assert tasks == [{"id": "T1", "name": "Task 1"}]
    assert calls[0] == [
        str(tmp_path / ".venv" / "bin" / "harness"), "clickup", "tasks", "--list-id", "L1",
    ]


def test_get_clickup_task_builds_correct_command(tmp_path, monkeypatch):
    calls = []

    def fake_run(command, cwd, capture_output, text):
        calls.append(list(command))
        return Result(stdout=json.dumps({"id": "T1", "name": "Task 1", "text_content": "desc"}))

    monkeypatch.setattr("meta_harness.clickup_bridge.subprocess.run", fake_run)

    task = get_clickup_task("T1", project_path=tmp_path)

    assert task == {"id": "T1", "name": "Task 1", "text_content": "desc"}
    assert calls[0] == [
        str(tmp_path / ".venv" / "bin" / "harness"), "clickup", "get-task", "--task-id", "T1",
    ]


def test_get_clickup_task_raises_when_payload_is_not_an_object(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "meta_harness.clickup_bridge.subprocess.run",
        lambda *a, **k: Result(stdout=json.dumps(["not", "an", "object"])),
    )

    with pytest.raises(ClickUpReadError, match="Expected a JSON object"):
        get_clickup_task("T1", project_path=tmp_path)


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
