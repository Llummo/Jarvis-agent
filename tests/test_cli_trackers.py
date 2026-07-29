"""The `meta-harness clickup` / `meta-harness linear` command groups.

These drive the same bridges the web UI uses, so the assertions here are
about argument handling — priority words mapping to the tracker's native
integers, comma-separated assignees becoming ints, optional flags staying
absent when not passed.
"""

import pytest
from click.testing import CliRunner

from meta_harness.cli import main


class FakeClickUpClient:
    def __init__(self):
        self.calls = []
        self.status_calls = []
        self.update_calls = []

    def create_task(self, list_id, name, description=None, **fields):
        self.calls.append({"list_id": list_id, "name": name, "description": description, **fields})
        return {"id": "task1", "name": name}

    def update_task_status(self, task_id, status):
        self.status_calls.append({"task_id": task_id, "status": status})
        return {"id": task_id, "status": {"status": status}}

    def update_task(self, task_id, *, name=None, description=None, **fields):
        self.update_calls.append({"task_id": task_id, "name": name, "description": description})
        return {"id": task_id, "name": name}

    def get_teams(self):
        return [{"id": "T1", "name": "Team 1"}]

    def get_spaces(self, team_id):
        return [{"id": "S1", "team_id": team_id}]


class FakeLinearClient:
    def __init__(self):
        self.calls = []
        self.state_calls = []

    def create_issue(self, team_id, title, description=None, **fields):
        self.calls.append({"team_id": team_id, "title": title, "description": description, **fields})
        return {"id": "issue1", "identifier": "TEA-1"}

    def update_issue_state(self, issue_id, state_id):
        self.state_calls.append({"issue_id": issue_id, "state_id": state_id})
        return {"id": issue_id, "state": {"id": state_id, "name": "Done"}}

    def get_viewer(self):
        return {"viewer": {"id": "U1", "name": "Me"}}


@pytest.fixture(autouse=True)
def tracker_env(monkeypatch):
    monkeypatch.setenv("CLICKUP_API_TOKEN", "test-token")
    monkeypatch.setenv("LINEAR_API_KEY", "test-key")
    monkeypatch.delenv("CLICKUP_TEAM_ID", raising=False)
    monkeypatch.delenv("CLICKUP_LIST_ID", raising=False)


@pytest.fixture
def clickup_client(monkeypatch):
    client = FakeClickUpClient()
    monkeypatch.setattr("meta_harness.clickup_bridge._client", lambda: client)
    return client


@pytest.fixture
def linear_client(monkeypatch):
    client = FakeLinearClient()
    monkeypatch.setattr("meta_harness.linear_bridge._client", lambda: client)
    return client


def _create_task(extra_args):
    return CliRunner().invoke(
        main, ["clickup", "create-task", "--list-id", "L1", "--name", "Do the thing", *extra_args]
    )


@pytest.mark.parametrize(
    "word,expected", [("urgent", 1), ("high", 2), ("normal", 3), ("low", 4)]
)
def test_create_task_maps_priority_word_to_clickup_integer(clickup_client, word, expected):
    result = _create_task(["--priority", word])

    assert result.exit_code == 0
    assert clickup_client.calls[0]["priority"] == expected


def test_create_task_without_priority_sends_no_priority_field(clickup_client):
    result = _create_task([])

    assert result.exit_code == 0
    assert "priority" not in clickup_client.calls[0]


def test_create_task_rejects_invalid_priority(clickup_client):
    result = _create_task(["--priority", "urgentish"])

    assert result.exit_code != 0


def test_create_task_parses_assignees_into_int_list(clickup_client):
    result = _create_task(["--assignees", "123,456"])

    assert result.exit_code == 0
    assert clickup_client.calls[0]["assignees"] == [123, 456]


def test_create_task_single_assignee(clickup_client):
    result = _create_task(["--assignees", "123"])

    assert result.exit_code == 0
    assert clickup_client.calls[0]["assignees"] == [123]


def test_create_task_without_assignees_sends_no_assignees_field(clickup_client):
    result = _create_task([])

    assert result.exit_code == 0
    assert "assignees" not in clickup_client.calls[0]


def test_create_task_passes_due_date(clickup_client):
    result = _create_task(["--due-date", "1735689600000"])

    assert result.exit_code == 0
    assert clickup_client.calls[0]["due_date"] == 1735689600000


def test_create_task_without_due_date_sends_no_due_date_field(clickup_client):
    result = _create_task([])

    assert result.exit_code == 0
    assert "due_date" not in clickup_client.calls[0]


def test_create_task_passes_parent(clickup_client):
    result = _create_task(["--parent", "T-parent"])

    assert result.exit_code == 0
    assert clickup_client.calls[0]["parent"] == "T-parent"


def test_create_task_falls_back_to_env_list_id(clickup_client, monkeypatch):
    monkeypatch.setenv("CLICKUP_LIST_ID", "env-list")

    result = CliRunner().invoke(main, ["clickup", "create-task", "--name", "Do the thing"])

    assert result.exit_code == 0
    assert clickup_client.calls[0]["list_id"] == "env-list"


def test_set_status_calls_client_with_task_id_and_status(clickup_client):
    result = CliRunner().invoke(
        main, ["clickup", "set-status", "--task-id", "T1", "--status", "done"]
    )

    assert result.exit_code == 0
    assert clickup_client.status_calls == [{"task_id": "T1", "status": "done"}]
    assert '"status": "done"' in result.output


def test_update_task_requires_a_field(clickup_client):
    result = CliRunner().invoke(main, ["clickup", "update-task", "--task-id", "T1"])

    assert result.exit_code != 0
    assert "Provide --name and/or --description" in result.output


def test_spaces_requires_a_team_id_when_env_is_empty(clickup_client):
    result = CliRunner().invoke(main, ["clickup", "spaces"])

    assert result.exit_code != 0
    assert "Provide --team-id" in result.output


def test_spaces_falls_back_to_env_team_id(clickup_client, monkeypatch):
    monkeypatch.setenv("CLICKUP_TEAM_ID", "env-team")

    result = CliRunner().invoke(main, ["clickup", "spaces"])

    assert result.exit_code == 0
    assert '"team_id": "env-team"' in result.output


def test_clickup_teams_emits_json(clickup_client):
    result = CliRunner().invoke(main, ["clickup", "teams"])

    assert result.exit_code == 0
    assert '"name": "Team 1"' in result.output


@pytest.mark.parametrize(
    "word,expected", [("urgent", 1), ("high", 2), ("normal", 3), ("low", 4)]
)
def test_create_issue_maps_priority_word_to_linear_integer(linear_client, word, expected):
    result = CliRunner().invoke(
        main,
        ["linear", "create-issue", "--team-id", "T1", "--title", "Fix", "--priority", word],
    )

    assert result.exit_code == 0
    assert linear_client.calls[0]["priority"] == expected


def test_create_issue_passes_parent_id(linear_client):
    result = CliRunner().invoke(
        main,
        ["linear", "create-issue", "--team-id", "T1", "--title", "Fix", "--parent-id", "I0"],
    )

    assert result.exit_code == 0
    assert linear_client.calls[0]["parent_id"] == "I0"


def test_linear_set_state_calls_client(linear_client):
    result = CliRunner().invoke(
        main, ["linear", "set-state", "--issue-id", "I1", "--state-id", "S2"]
    )

    assert result.exit_code == 0
    assert linear_client.state_calls == [{"issue_id": "I1", "state_id": "S2"}]


def test_linear_update_issue_requires_a_field(linear_client):
    result = CliRunner().invoke(main, ["linear", "update-issue", "--issue-id", "I1"])

    assert result.exit_code != 0
    assert "Provide --title and/or --description" in result.output


def test_linear_viewer_emits_json(linear_client):
    result = CliRunner().invoke(main, ["linear", "viewer"])

    assert result.exit_code == 0
    assert '"name": "Me"' in result.output


def test_bad_credentials_report_a_clean_error_not_a_traceback(monkeypatch):
    """A rejected token is an expected outcome — it should read as a CLI
    error, not dump the bridge's stack."""
    from meta_harness.clickup_bridge import ClickUpReadError

    def boom():
        raise ClickUpReadError("ClickUp team listing failed: ClickUp API error 401")

    monkeypatch.setattr("meta_harness.cli.list_clickup_teams", boom)

    result = CliRunner().invoke(main, ["clickup", "teams"])

    assert result.exit_code == 1
    assert "Error: ClickUp team listing failed" in result.output
    assert "Traceback" not in result.output


def test_linear_errors_report_cleanly(monkeypatch):
    from meta_harness.linear_bridge import LinearReadError

    def boom():
        raise LinearReadError("Linear team listing failed: Linear API error 401")

    monkeypatch.setattr("meta_harness.cli.list_linear_teams", boom)

    result = CliRunner().invoke(main, ["linear", "teams"])

    assert result.exit_code == 1
    assert "Error: Linear team listing failed" in result.output
    assert "Traceback" not in result.output
