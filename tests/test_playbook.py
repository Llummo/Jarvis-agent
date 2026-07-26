import json
from pathlib import Path

import pytest

from meta_harness.playbook import (
    AgentPlaybook,
    flow_requires_subject,
    init_project,
    list_agent_playbooks,
    load_agent_playbook,
    replay_run,
    resolve_project_path,
    run_and_record_flow,
    run_complete,
    run_flow,
)
from meta_harness.run_archive import RunArchive


def _write_playbook(agents_dir: Path, payload: dict) -> Path:
    agents_dir.mkdir(parents=True, exist_ok=True)
    path = agents_dir / f"{payload['name']}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _playbook(**overrides) -> AgentPlaybook:
    defaults = dict(
        name="clickup",
        description="test agent",
        project_env_var="CLICKUP_PROJECT_REPO",
        project_sibling="p-harness",
        setup=[["python3", "-m", "venv", ".venv"]],
        flow=[[".venv/bin/harness", "clickup", "teams"]],
        env_example=".env.example",
        env_file=".env",
    )
    defaults.update(overrides)
    return AgentPlaybook(**defaults)


def test_load_agent_playbook(tmp_path, monkeypatch):
    agents_dir = tmp_path / "agents"
    _write_playbook(agents_dir, {
        "name": "clickup",
        "description": "ClickUp harness",
        "project_env_var": "CLICKUP_PROJECT_REPO",
        "project_sibling": "p-harness",
        "setup": [["python3", "-m", "venv", ".venv"]],
        "flow": [[".venv/bin/harness", "clickup", "teams"]],
    })
    monkeypatch.setattr("meta_harness.playbook.agents_dir", lambda: agents_dir)

    playbook = load_agent_playbook("clickup")

    assert playbook.name == "clickup"
    assert playbook.project_sibling == "p-harness"
    assert playbook.setup == [["python3", "-m", "venv", ".venv"]]


def test_load_agent_playbook_missing_raises(tmp_path, monkeypatch):
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    monkeypatch.setattr("meta_harness.playbook.agents_dir", lambda: agents_dir)

    with pytest.raises(FileNotFoundError, match="No playbook found for agent 'nope'"):
        load_agent_playbook("nope")


def test_list_agent_playbooks(tmp_path, monkeypatch):
    agents_dir = tmp_path / "agents"
    _write_playbook(agents_dir, {
        "name": "alpha",
        "project_env_var": "ALPHA_REPO",
        "project_sibling": "alpha-project",
    })
    _write_playbook(agents_dir, {
        "name": "beta",
        "project_env_var": "BETA_REPO",
        "project_sibling": "beta-project",
    })
    monkeypatch.setattr("meta_harness.playbook.agents_dir", lambda: agents_dir)

    names = [playbook.name for playbook in list_agent_playbooks()]

    assert names == ["alpha", "beta"]


def test_list_agent_playbooks_missing_dir_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr("meta_harness.playbook.agents_dir", lambda: tmp_path / "does-not-exist")

    assert list_agent_playbooks() == []


def test_resolve_project_path_prefers_env_var(tmp_path, monkeypatch):
    explicit = tmp_path / "explicit-project"
    explicit.mkdir()
    monkeypatch.setenv("CLICKUP_PROJECT_REPO", str(explicit))

    resolved = resolve_project_path(_playbook(), search_root=tmp_path)

    assert resolved == explicit.resolve()


def test_resolve_project_path_falls_back_to_sibling(tmp_path, monkeypatch):
    monkeypatch.delenv("CLICKUP_PROJECT_REPO", raising=False)
    sibling = tmp_path / "p-harness"
    sibling.mkdir()

    resolved = resolve_project_path(_playbook(), search_root=tmp_path)

    assert resolved == sibling.resolve()


def test_resolve_project_path_raises_when_not_found(tmp_path, monkeypatch):
    monkeypatch.delenv("CLICKUP_PROJECT_REPO", raising=False)

    with pytest.raises(FileNotFoundError, match="Could not locate project for agent 'clickup'"):
        resolve_project_path(_playbook(), search_root=tmp_path)


def test_init_project_runs_setup_and_copies_env_file(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env.example").write_text("TOKEN=xxx\n", encoding="utf-8")

    calls = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(command, cwd, capture_output, text):
        calls.append((list(command), Path(cwd)))
        return Result()

    monkeypatch.setattr("meta_harness.playbook.subprocess.run", fake_run)

    results = init_project(_playbook(), project_path=project)

    assert [r.command for r in results] == [["python3", "-m", "venv", ".venv"]]
    assert calls == [(["python3", "-m", "venv", ".venv"], project)]
    assert (project / ".env").read_text(encoding="utf-8") == "TOKEN=xxx\n"


def test_init_project_does_not_overwrite_existing_env_file(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env.example").write_text("TOKEN=xxx\n", encoding="utf-8")
    (project / ".env").write_text("TOKEN=real\n", encoding="utf-8")

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr("meta_harness.playbook.subprocess.run", lambda *a, **k: Result())

    init_project(_playbook(), project_path=project)

    assert (project / ".env").read_text(encoding="utf-8") == "TOKEN=real\n"


def test_init_project_stops_on_first_failure(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()

    class FailResult:
        returncode = 1
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr("meta_harness.playbook.subprocess.run", lambda *a, **k: FailResult())

    playbook = _playbook(setup=[["step-one"], ["step-two"]])
    results = init_project(playbook, project_path=project)

    assert len(results) == 1
    assert not results[0].ok


def test_run_flow_executes_all_steps(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()

    class Result:
        returncode = 0
        stdout = "ok"
        stderr = ""

    monkeypatch.setattr("meta_harness.playbook.subprocess.run", lambda *a, **k: Result())

    results = run_flow(_playbook(), project_path=project)

    assert len(results) == 1
    assert results[0].ok


def test_run_complete_skips_flow_when_setup_fails(tmp_path, monkeypatch):
    monkeypatch.delenv("CLICKUP_PROJECT_REPO", raising=False)
    project = tmp_path / "p-harness"
    project.mkdir()

    class FailResult:
        returncode = 1
        stdout = ""
        stderr = "setup broke"

    monkeypatch.setattr("meta_harness.playbook.subprocess.run", lambda *a, **k: FailResult())
    monkeypatch.setattr("meta_harness.playbook._default_search_root", lambda: tmp_path)

    setup_results, flow_results = run_complete(_playbook())

    assert setup_results and not setup_results[-1].ok
    assert flow_results == []


def _subject_playbook(**overrides) -> AgentPlaybook:
    return _playbook(flow=[[".venv/bin/harness", "clickup", "get-task", "--task-id", "{subject_id}"]], **overrides)


def test_flow_requires_subject_true_when_placeholder_present():
    assert flow_requires_subject(_subject_playbook()) is True


def test_flow_requires_subject_false_for_static_flow():
    assert flow_requires_subject(_playbook(flow=[["harness", "clickup", "teams"]])) is False


def test_run_flow_raises_without_subject_when_required(tmp_path):
    with pytest.raises(ValueError, match="pass subject_id"):
        run_flow(_subject_playbook(), project_path=tmp_path)


def test_run_flow_substitutes_subject_id(tmp_path, monkeypatch):
    calls = []

    class Result:
        returncode = 0
        stdout = "{}"
        stderr = ""

    def fake_run(command, cwd, capture_output, text):
        calls.append(list(command))
        return Result()

    monkeypatch.setattr("meta_harness.playbook.subprocess.run", fake_run)

    run_flow(_subject_playbook(), project_path=tmp_path, subject_id="ticket-123")

    assert calls == [[".venv/bin/harness", "clickup", "get-task", "--task-id", "ticket-123"]]


def test_run_and_record_flow_persists_to_archive(tmp_path, monkeypatch):
    class Result:
        returncode = 0
        stdout = '{"id": "ticket-123"}'
        stderr = ""

    monkeypatch.setattr("meta_harness.playbook.subprocess.run", lambda *a, **k: Result())
    archive = RunArchive("clickup", directory=tmp_path)

    results, record = run_and_record_flow(
        _subject_playbook(), subject_id="ticket-123", project_path=tmp_path, archive=archive
    )

    assert results[0].ok
    assert record.subject_id == "ticket-123"
    assert record.ok is True
    stored = archive.load()
    assert len(stored) == 1
    assert stored[0].run_id == record.run_id


def test_replay_run_reruns_same_subject(tmp_path, monkeypatch):
    seen_subjects = []

    class Result:
        returncode = 0
        stdout = "{}"
        stderr = ""

    def fake_run(command, cwd, capture_output, text):
        seen_subjects.append(command[-1])
        return Result()

    monkeypatch.setattr("meta_harness.playbook.subprocess.run", fake_run)
    monkeypatch.setattr("meta_harness.playbook.resolve_project_path", lambda playbook: tmp_path)
    archive = RunArchive("clickup", directory=tmp_path)

    playbook = _subject_playbook()
    _, original_record = run_and_record_flow(
        playbook, subject_id="ticket-123", project_path=tmp_path, archive=archive
    )

    original, results, replayed = replay_run(playbook, original_record.run_id, archive=archive)

    assert original.run_id == original_record.run_id
    assert results[0].ok
    assert replayed.subject_id == "ticket-123"
    assert replayed.run_id != original.run_id
    assert seen_subjects == ["ticket-123", "ticket-123"]
    assert len(archive.load()) == 2


def test_replay_run_missing_run_id_raises(tmp_path):
    archive = RunArchive("clickup", directory=tmp_path)

    with pytest.raises(FileNotFoundError, match="No recorded run 'missing'"):
        replay_run(_subject_playbook(), "missing", archive=archive)
