"""Reformatting overwrites text a person wrote, so it must be undoable."""

import pytest

from meta_harness.reformat_history import ReformatHistoryStore
from meta_harness.ticket_reformat import (
    NoPreviousVersionError,
    apply_reformatted_ticket,
    revert_ticket,
)


def _store(tmp_path):
    return ReformatHistoryStore(tmp_path / "history.json")


def _mock_fetch(monkeypatch, name="Titulo viejo", description="Cuerpo viejo"):
    monkeypatch.setattr(
        "meta_harness.ticket_reformat.get_clickup_task",
        lambda ticket_id, **kw: {"id": ticket_id, "name": name, "text_content": description},
    )
    monkeypatch.setattr(
        "meta_harness.ticket_reformat.get_linear_issue",
        lambda issue_id, **kw: {"id": issue_id, "title": name, "description": description},
    )


# ---------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------


def test_remembers_and_returns_a_previous_version(tmp_path):
    store = _store(tmp_path)
    store.remember("T1", "clickup", title="viejo", description="cuerpo")

    entry = store.get("T1", "clickup")

    assert entry["title"] == "viejo"
    assert entry["description"] == "cuerpo"
    assert entry["saved_at"]


def test_trackers_do_not_collide_on_the_same_id(tmp_path):
    store = _store(tmp_path)
    store.remember("X", "clickup", title="de clickup", description="a")
    store.remember("X", "linear", title="de linear", description="b")

    assert store.get("X", "clickup")["title"] == "de clickup"
    assert store.get("X", "linear")["title"] == "de linear"


def test_forget_removes_the_entry(tmp_path):
    store = _store(tmp_path)
    store.remember("T1", "clickup", title="v", description="c")
    store.forget("T1", "clickup")

    assert store.get("T1", "clickup") is None


def test_list_for_filters_by_tracker_newest_first(tmp_path):
    store = _store(tmp_path)
    store.remember("A", "clickup", title="a", description="")
    store.remember("B", "linear", title="b", description="")
    store.remember("C", "clickup", title="c", description="")

    assert {e["ticket_id"] for e in store.list_for("clickup")} == {"A", "C"}
    assert [e["ticket_id"] for e in store.list_for("linear")] == ["B"]
    assert len(store.list_for()) == 3


def test_a_corrupt_history_file_does_not_explode(tmp_path):
    path = tmp_path / "history.json"
    path.write_text("{ not json", encoding="utf-8")

    # Losing undo is bad; refusing to work at all is worse.
    assert ReformatHistoryStore(path).get("T1", "clickup") is None


# ---------------------------------------------------------------------------
# apply saves an undo point
# ---------------------------------------------------------------------------


def test_apply_saves_the_version_it_is_about_to_overwrite(tmp_path, monkeypatch):
    _mock_fetch(monkeypatch, name="Titulo original", description="Cuerpo original")
    monkeypatch.setattr(
        "meta_harness.ticket_reformat.update_clickup_task",
        lambda task_id, **kw: {"id": task_id, "name": kw.get("name")},
    )
    store = _store(tmp_path)

    apply_reformatted_ticket(
        "T1", tracker="clickup", title="Titulo nuevo", description="Cuerpo nuevo", history=store
    )

    saved = store.get("T1", "clickup")
    assert saved["title"] == "Titulo original"
    assert saved["description"] == "Cuerpo original"
    assert saved["new_title"] == "Titulo nuevo"


def test_apply_still_updates_when_the_undo_point_cannot_be_saved(tmp_path, monkeypatch):
    # A history failure must never block the user's actual request.
    def boom(*a, **kw):
        raise RuntimeError("tracker unreachable")

    monkeypatch.setattr("meta_harness.ticket_reformat.get_clickup_task", boom)
    calls = []
    monkeypatch.setattr(
        "meta_harness.ticket_reformat.update_clickup_task",
        lambda task_id, **kw: calls.append(task_id) or {"id": task_id},
    )

    apply_reformatted_ticket("T1", tracker="clickup", title="n", description="c", history=_store(tmp_path))

    assert calls == ["T1"]


# ---------------------------------------------------------------------------
# revert
# ---------------------------------------------------------------------------


def test_revert_writes_back_the_exact_saved_text(tmp_path, monkeypatch):
    store = _store(tmp_path)
    store.remember("T1", "clickup", title="Titulo original", description="Cuerpo original")
    captured = {}
    monkeypatch.setattr(
        "meta_harness.ticket_reformat.update_clickup_task",
        lambda task_id, **kw: captured.update(kw, task_id=task_id) or {"id": task_id},
    )

    revert_ticket("T1", tracker="clickup", history=store)

    assert captured["name"] == "Titulo original"
    assert captured["description"] == "Cuerpo original"


def test_revert_uses_the_linear_updater_for_linear(tmp_path, monkeypatch):
    store = _store(tmp_path)
    store.remember("I1", "linear", title="Titulo", description="Cuerpo")
    captured = {}
    monkeypatch.setattr(
        "meta_harness.ticket_reformat.update_linear_issue",
        lambda issue_id, **kw: captured.update(kw, issue_id=issue_id) or {"id": issue_id},
    )

    def boom(*a, **kw):
        raise AssertionError("must not touch ClickUp for a Linear issue")

    monkeypatch.setattr("meta_harness.ticket_reformat.update_clickup_task", boom)

    revert_ticket("I1", tracker="linear", history=store)

    assert captured["title"] == "Titulo"


def test_revert_clears_the_saved_version_so_it_is_offered_once(tmp_path, monkeypatch):
    store = _store(tmp_path)
    store.remember("T1", "clickup", title="v", description="c")
    monkeypatch.setattr("meta_harness.ticket_reformat.update_clickup_task", lambda t, **kw: {"id": t})

    revert_ticket("T1", tracker="clickup", history=store)

    assert store.get("T1", "clickup") is None
    with pytest.raises(NoPreviousVersionError):
        revert_ticket("T1", tracker="clickup", history=store)


def test_revert_without_a_saved_version_raises(tmp_path):
    with pytest.raises(NoPreviousVersionError, match="No saved version"):
        revert_ticket("T1", tracker="clickup", history=_store(tmp_path))


def test_revert_rejects_an_unknown_tracker(tmp_path):
    with pytest.raises(ValueError, match="Invalid tracker"):
        revert_ticket("T1", tracker="jira", history=_store(tmp_path))


def test_apply_then_revert_round_trips(tmp_path, monkeypatch):
    _mock_fetch(monkeypatch, name="ORIGINAL", description="TEXTO ORIGINAL")
    written = {}
    monkeypatch.setattr(
        "meta_harness.ticket_reformat.update_clickup_task",
        lambda task_id, **kw: written.update(kw) or {"id": task_id},
    )
    store = _store(tmp_path)

    apply_reformatted_ticket("T1", tracker="clickup", title="NUEVO", description="TEXTO NUEVO", history=store)
    assert written["name"] == "NUEVO"

    revert_ticket("T1", tracker="clickup", history=store)
    assert written["name"] == "ORIGINAL"
    assert written["description"] == "TEXTO ORIGINAL"
