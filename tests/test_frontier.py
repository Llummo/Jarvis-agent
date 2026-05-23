from pathlib import Path

from meta_harness.frontier import FrontierStore
from meta_harness.models import RunSummary


def test_frontier_upsert_and_best(tmp_path):
    frontier = FrontierStore(tmp_path / "frontier.json")

    baseline = RunSummary(
        benchmark_name="tblite",
        candidate_name="baseline",
        candidate_path="/tmp/baseline.py",
        run_dir=Path("/tmp/base"),
        eval_metrics={"eval/pass_rate": 0.2, "eval/total_tasks": 10},
        manifest={"outer_loop": {"benchmark_runner": {"task_selection": {"selection_hash": "hash_a"}}}},
    )
    improved = RunSummary(
        benchmark_name="tblite",
        candidate_name="candidate",
        candidate_path="/tmp/candidate.py",
        run_dir=Path("/tmp/candidate"),
        eval_metrics={"eval/pass_rate": 0.4, "eval/total_tasks": 10},
        manifest={"outer_loop": {"benchmark_runner": {"task_selection": {"selection_hash": "hash_a"}}}},
    )

    frontier.upsert_from_summary(baseline)
    frontier.upsert_from_summary(improved)

    best = frontier.best_for_benchmark("tblite")
    assert best.candidate_name == "candidate"
    assert best.pass_rate == 0.4


def test_frontier_top_for_benchmark_orders_entries(tmp_path):
    frontier = FrontierStore(tmp_path / "frontier.json")

    for name, pass_rate, total_tasks in [("alpha", 0.1, 50), ("gamma", 0.7, 20), ("beta", 0.7, 50)]:
        frontier.upsert_from_summary(
            RunSummary(
                benchmark_name="tblite",
                candidate_name=name,
                candidate_path=f"/tmp/{name}.py",
                run_dir=Path(f"/tmp/{name}"),
                eval_metrics={"eval/pass_rate": pass_rate, "eval/total_tasks": total_tasks},
                manifest={"outer_loop": {"benchmark_runner": {"task_selection": {"selection_hash": f"hash_{name}"}}}},
            )
        )

    ranked = frontier.top_for_benchmark("tblite", limit=3)
    assert [entry.candidate_name for entry in ranked] == ["beta", "alpha", "gamma"]
    assert [entry.total_tasks for entry in ranked] == [50, 50, 20]


def test_frontier_top_for_benchmark_filters_by_task_selection_hash(tmp_path):
    frontier = FrontierStore(tmp_path / "frontier.json")

    for name, selection_hash in [("alpha", "hash_a"), ("beta", "hash_b")]:
        frontier.upsert_from_summary(
            RunSummary(
                benchmark_name="tblite",
                candidate_name=name,
                candidate_path=f"/tmp/{name}.py",
                run_dir=Path(f"/tmp/{name}"),
                eval_metrics={"eval/pass_rate": 0.5, "eval/total_tasks": 10},
                manifest={"outer_loop": {"benchmark_runner": {"task_selection": {"selection_hash": selection_hash}}}},
            )
        )

    ranked = frontier.top_for_benchmark("tblite", task_selection_hash="hash_b")
    assert [entry.candidate_name for entry in ranked] == ["beta"]


def test_frontier_keeps_same_candidate_across_task_selections(tmp_path):
    frontier = FrontierStore(tmp_path / "frontier.json")

    for selection_hash, pass_rate in [("subset_a", 0.4), ("subset_b", 0.6)]:
        frontier.upsert_from_summary(
            RunSummary(
                benchmark_name="tblite",
                candidate_name="candidate",
                candidate_path="/tmp/candidate.py",
                run_dir=Path(f"/tmp/{selection_hash}"),
                eval_metrics={"eval/pass_rate": pass_rate, "eval/total_tasks": 10},
                manifest={"outer_loop": {"benchmark_runner": {"task_selection": {"selection_hash": selection_hash}}}},
            )
        )

    entries = frontier.load()
    assert len(entries) == 2
    assert {entry.task_selection_hash for entry in entries} == {"subset_a", "subset_b"}


def test_frontier_load_rejects_malformed_json(tmp_path):
    path = tmp_path / "frontier.json"
    path.write_text("{not json", encoding="utf-8")

    frontier = FrontierStore(path)
    try:
        frontier.load()
    except ValueError as exc:
        assert "Malformed frontier JSON" in str(exc)
    else:
        raise AssertionError("Expected malformed JSON to raise ValueError")


def test_frontier_load_rejects_non_list_payload(tmp_path):
    path = tmp_path / "frontier.json"
    path.write_text('{"candidate_name": "x"}', encoding="utf-8")

    frontier = FrontierStore(path)
    try:
        frontier.load()
    except ValueError as exc:
        assert "must contain a JSON list" in str(exc)
    else:
        raise AssertionError("Expected non-list frontier payload to raise ValueError")


def test_frontier_load_rejects_invalid_entry_shape(tmp_path):
    path = tmp_path / "frontier.json"
    path.write_text('[{"candidate_name": "missing required fields"}]', encoding="utf-8")

    frontier = FrontierStore(path)
    try:
        frontier.load()
    except ValueError as exc:
        assert "missing required fields" in str(exc)
    else:
        raise AssertionError("Expected invalid frontier entry to raise ValueError")
