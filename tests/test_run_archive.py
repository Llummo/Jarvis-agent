import pytest

from meta_harness.run_archive import RunArchive, RunRecord, RunStepRecord


def _record(run_id="run1", subject_id="ticket1", ok=True) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        agent="clickup",
        subject_id=subject_id,
        started_at="2026-07-26T00:00:00+00:00",
        ok=ok,
        steps=[RunStepRecord(command=["harness", "clickup", "get-task"], returncode=0, stdout="{}", stderr="")],
    )


def test_append_and_load_round_trip(tmp_path):
    archive = RunArchive("clickup", directory=tmp_path)

    archive.append(_record())

    loaded = archive.load()
    assert len(loaded) == 1
    assert loaded[0].run_id == "run1"
    assert loaded[0].subject_id == "ticket1"
    assert loaded[0].steps[0].command == ["harness", "clickup", "get-task"]
    assert loaded[0].steps[0].ok is True


def test_load_missing_archive_returns_empty(tmp_path):
    archive = RunArchive("clickup", directory=tmp_path)

    assert archive.load() == []


def test_append_preserves_existing_records(tmp_path):
    archive = RunArchive("clickup", directory=tmp_path)
    archive.append(_record(run_id="run1"))
    archive.append(_record(run_id="run2", subject_id="ticket2"))

    loaded = archive.load()

    assert [r.run_id for r in loaded] == ["run1", "run2"]


def test_get_returns_matching_record(tmp_path):
    archive = RunArchive("clickup", directory=tmp_path)
    archive.append(_record(run_id="run1"))
    archive.append(_record(run_id="run2", subject_id="ticket2"))

    found = archive.get("run2")

    assert found.subject_id == "ticket2"


def test_get_missing_run_raises(tmp_path):
    archive = RunArchive("clickup", directory=tmp_path)

    with pytest.raises(FileNotFoundError, match="No recorded run 'missing'"):
        archive.get("missing")
