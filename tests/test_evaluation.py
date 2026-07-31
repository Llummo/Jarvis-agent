import json

import pytest

from meta_harness.archive_reader import load_run_summary
from meta_harness.comparison import build_comparison_report
from meta_harness.evaluation import runner as runner_module
from meta_harness.evaluation.candidates import Candidate, CandidateError, baseline, dedupe
from meta_harness.evaluation.dataset import Dataset, DatasetError, LabelledTask, template
from meta_harness.evaluation.runner import evaluate_candidate
from meta_harness.module_relevance import ModuleRelevance


def _task(name, verdict, ticket=None):
    return LabelledTask(
        task_name=name, ticket_id=ticket or f"TAS-{name}", expected_verdict=verdict
    )


def _dataset(tasks=None, **overrides):
    # `is None`, not `or` — an explicitly empty task list is a case under test.
    if tasks is None:
        tasks = [
            _task("t1", "related"),
            _task("t2", "unrelated"),
            _task("t3", "partially_related"),
        ]
    payload = {
        "name": "billing-relevance",
        "capability": "module_relevance",
        "module_name": "billing",
        "module_context": "The billing module issues invoices.",
        "tasks": tasks,
    }
    payload.update(overrides)
    return Dataset(**payload)


def _relevance(verdict, confidence=0.9):
    return ModuleRelevance(
        ticket_id="TAS-1",
        ticket_name="Some ticket",
        module_name="billing",
        verdict=verdict,
        confidence=confidence,
        rationale="because",
        matched_aspects=[],
        module_gaps=[],
    )


# --- dataset -----------------------------------------------------------------


def test_dataset_rejects_unknown_verdict():
    with pytest.raises(DatasetError, match="expected_verdict"):
        _dataset(tasks=[_task("t1", "sort_of")]).validate()


def test_dataset_rejects_unknown_tracker():
    task = LabelledTask("t1", "TAS-1", "related", tracker="jira")
    with pytest.raises(DatasetError, match="tracker"):
        _dataset(tasks=[task]).validate()


def test_dataset_rejects_duplicate_task_names():
    tasks = [_task("same", "related"), _task("same", "unrelated")]
    with pytest.raises(DatasetError, match="duplicate"):
        _dataset(tasks=tasks).validate()


def test_dataset_rejects_comma_in_task_name():
    # A comma would split one task into two inside the selection hash.
    with pytest.raises(DatasetError, match="comma"):
        _dataset(tasks=[_task("a,b", "related")]).validate()


def test_dataset_requires_tasks():
    with pytest.raises(DatasetError, match="no tasks"):
        _dataset(tasks=[]).validate()


def test_dataset_requires_repo_or_context():
    dataset = _dataset(module_context="")
    with pytest.raises(DatasetError, match="repo"):
        dataset.validate()


def test_dataset_accepts_repo_without_pasted_context():
    _dataset(module_context="", repo="sigo").validate()


def test_label_counts_covers_every_verdict():
    counts = _dataset().label_counts
    assert counts == {"related": 1, "unrelated": 1, "partially_related": 1}


def test_imbalance_warning_flags_a_dominant_label():
    tasks = [_task(f"t{i}", "unrelated") for i in range(9)] + [_task("t9", "related")]
    warning = _dataset(tasks=tasks).imbalance_warning()
    assert warning is not None
    assert "90%" in warning


def test_imbalance_warning_silent_when_balanced():
    assert _dataset().imbalance_warning() is None


def test_dataset_round_trips_through_disk(tmp_path):
    original = _dataset()
    path = original.save(tmp_path / "set.json")
    assert Dataset.load(path).to_dict() == original.to_dict()


def test_dataset_load_rejects_unknown_task_fields(tmp_path):
    path = tmp_path / "set.json"
    payload = _dataset().to_dict()
    payload["tasks"][0]["exepcted_verdict"] = "related"  # typo a human would make
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(DatasetError, match="unknown fields"):
        Dataset.load(path)


def test_dataset_load_reports_bad_json(tmp_path):
    path = tmp_path / "set.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(DatasetError, match="not valid JSON"):
        Dataset.load(path)


def test_template_is_valid_but_needs_a_real_ticket():
    dataset = template("starter", "billing", repo="sigo")
    dataset.validate()
    assert "REPLACE" in dataset.tasks[0].ticket_id


# --- candidates ---------------------------------------------------------------


def test_baseline_is_valid():
    baseline().validate()


def test_candidate_rejects_out_of_range_context_limit():
    with pytest.raises(CandidateError, match="context_limit"):
        Candidate(name="wide", context_limit=999).validate()


def test_candidate_rejects_non_positive_timeout():
    with pytest.raises(CandidateError, match="timeout_s"):
        Candidate(name="quick", timeout_s=0).validate()


def test_render_context_applies_preamble():
    candidate = Candidate(name="framed", context_preamble="Judge strictly.")
    assert candidate.render_context("  body  ") == "Judge strictly.\n\nbody"


def test_render_context_without_preamble_is_just_the_body():
    assert baseline().render_context("  body  ") == "body"


def test_fingerprint_ignores_the_name():
    assert Candidate(name="a").fingerprint() == Candidate(name="b").fingerprint()


def test_fingerprint_changes_with_settings():
    assert Candidate(name="a").fingerprint() != Candidate(name="a", context_limit=5).fingerprint()


def test_dedupe_drops_renamed_duplicates():
    unique = dedupe([Candidate(name="a"), Candidate(name="b"), Candidate(name="c", context_limit=5)])
    assert [c.name for c in unique] == ["a", "c"]


def test_variant_validates_eagerly():
    with pytest.raises(CandidateError):
        baseline().variant("broken", context_limit=0)


def test_candidate_round_trips_through_describe():
    candidate = Candidate(name="framed", context_limit=20, verified_only=False, timeout_s=30.0)
    assert Candidate.from_dict(candidate.describe()) == candidate


def test_candidate_from_dict_rejects_unknown_fields():
    with pytest.raises(CandidateError, match="unknown"):
        Candidate.from_dict({"name": "a", "contextlimit": 5})


# --- runner -------------------------------------------------------------------


def test_run_dir_is_readable_by_the_existing_archive_reader(tmp_path, monkeypatch):
    """The whole point: the untouched Hermes-side reader consumes our output."""
    monkeypatch.setattr(
        runner_module, "analyze_module_relevance", lambda *a, **k: _relevance("related")
    )
    result = evaluate_candidate(baseline(), _dataset(), archive_root=tmp_path)

    summary = load_run_summary(result.run_dir)
    assert summary.benchmark_name == "billing-relevance"
    assert summary.candidate_name == "baseline"
    assert summary.eval_metrics["eval/total_tasks"] == 3
    assert len(summary.task_results) == 3
    assert {task["task_name"] for task in summary.task_results} == {"t1", "t2", "t3"}


def test_pass_rate_reflects_only_correct_verdicts(tmp_path, monkeypatch):
    # Always answers "related": correct for t1 only, out of three tasks.
    monkeypatch.setattr(
        runner_module, "analyze_module_relevance", lambda *a, **k: _relevance("related")
    )
    result = evaluate_candidate(baseline(), _dataset(), archive_root=tmp_path)
    assert result.pass_rate == pytest.approx(1 / 3)


def test_errored_task_is_not_counted_as_a_wrong_answer(tmp_path, monkeypatch):
    def flaky(ticket_id, **kwargs):
        if ticket_id == "TAS-t2":
            raise runner_module.ClickUpReadError("tracker down")
        return _relevance("related" if ticket_id == "TAS-t1" else "partially_related")

    monkeypatch.setattr(runner_module, "analyze_module_relevance", flaky)
    result = evaluate_candidate(baseline(), _dataset(), archive_root=tmp_path)

    # t1 and t3 answered correctly; t2 errored and is excluded from scoring.
    assert result.pass_rate == 1.0
    assert len(result.errors) == 1
    assert len(result.scored) == 2
    assert "tracker down" in result.errors[0].error


def test_run_with_only_errors_does_not_divide_by_zero(tmp_path, monkeypatch):
    def always_fails(*args, **kwargs):
        raise runner_module.ClickUpReadError("down")

    monkeypatch.setattr(runner_module, "analyze_module_relevance", always_fails)
    result = evaluate_candidate(baseline(), _dataset(), archive_root=tmp_path)
    assert result.pass_rate == 0.0
    assert result.scored == []


def test_per_verdict_recall_exposes_a_constant_answerer(tmp_path, monkeypatch):
    monkeypatch.setattr(
        runner_module, "analyze_module_relevance", lambda *a, **k: _relevance("unrelated")
    )
    result = evaluate_candidate(baseline(), _dataset(), archive_root=tmp_path)
    recall = result.per_verdict_recall()
    assert recall["unrelated"] == {"total": 1, "correct": 1}
    assert recall["related"] == {"total": 1, "correct": 0}
    assert result.confusions()["related->unrelated"] == 1


def test_candidate_retrieval_settings_reach_the_retriever(tmp_path, monkeypatch):
    seen = {}

    def fake_retrieve(module_name, **kwargs):
        seen.update(kwargs)
        return "retrieved source"

    captured = {}

    def fake_analyze(ticket_id, **kwargs):
        captured["module_context"] = kwargs["module_context"]
        return _relevance("related")

    monkeypatch.setattr(runner_module, "retrieve_module_context", fake_retrieve)
    monkeypatch.setattr(runner_module, "analyze_module_relevance", fake_analyze)

    candidate = Candidate(
        name="wide-unverified", context_limit=30, verified_only=False, context_preamble="Be strict."
    )
    evaluate_candidate(candidate, _dataset(module_context="", repo="sigo"), archive_root=tmp_path)

    assert seen["limit"] == 30
    assert seen["verified_only"] is False
    assert seen["repo"] == "sigo"
    assert captured["module_context"] == "Be strict.\n\nretrieved source"


def test_pasted_context_skips_retrieval(tmp_path, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("must not retrieve when context was supplied")

    monkeypatch.setattr(runner_module, "retrieve_module_context", explode)
    monkeypatch.setattr(
        runner_module, "analyze_module_relevance", lambda *a, **k: _relevance("related")
    )
    evaluate_candidate(baseline(), _dataset(), archive_root=tmp_path)


def test_context_is_retrieved_once_for_the_whole_run(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        runner_module,
        "retrieve_module_context",
        lambda module_name, **kwargs: calls.append(module_name) or "src",
    )
    monkeypatch.setattr(
        runner_module, "analyze_module_relevance", lambda *a, **k: _relevance("related")
    )
    evaluate_candidate(baseline(), _dataset(module_context="", repo="sigo"), archive_root=tmp_path)
    assert len(calls) == 1


def test_same_dataset_yields_a_matching_selection_hash(tmp_path, monkeypatch):
    monkeypatch.setattr(
        runner_module, "analyze_module_relevance", lambda *a, **k: _relevance("related")
    )
    dataset = _dataset()
    first = evaluate_candidate(baseline(), dataset, archive_root=tmp_path, run_name="a")
    second = evaluate_candidate(
        Candidate(name="rival"), dataset, archive_root=tmp_path, run_name="b"
    )

    report = build_comparison_report(load_run_summary(first.run_dir), load_run_summary(second.run_dir))
    assert report.task_selection_status =="matching"


def test_different_task_sets_are_flagged_as_not_comparable(tmp_path, monkeypatch):
    monkeypatch.setattr(
        runner_module, "analyze_module_relevance", lambda *a, **k: _relevance("related")
    )
    wide = _dataset()
    narrow = _dataset(tasks=[_task("t1", "related")])
    first = evaluate_candidate(baseline(), wide, archive_root=tmp_path, run_name="a")
    second = evaluate_candidate(baseline(), narrow, archive_root=tmp_path, run_name="b")

    report = build_comparison_report(load_run_summary(first.run_dir), load_run_summary(second.run_dir))
    assert report.task_selection_status =="mismatched"


def test_manifest_records_how_the_run_was_configured(tmp_path, monkeypatch):
    monkeypatch.setattr(
        runner_module, "analyze_module_relevance", lambda *a, **k: _relevance("related")
    )
    candidate = Candidate(name="wide", context_limit=25)
    result = evaluate_candidate(candidate, _dataset(), archive_root=tmp_path)

    manifest = json.loads((result.run_dir / "manifest.json").read_text(encoding="utf-8"))
    provenance = manifest["outer_loop"]["provenance"]
    assert provenance["candidate"]["context_limit"] == 25
    assert provenance["capability"] == "module_relevance"
    assert provenance["context_source"] == "pasted"
    assert provenance["label_counts"]["related"] == 1


def test_task_names_with_slashes_do_not_escape_the_tasks_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(
        runner_module, "analyze_module_relevance", lambda *a, **k: _relevance("related")
    )
    dataset = _dataset(tasks=[_task("../../escape", "related")])
    result = evaluate_candidate(baseline(), dataset, archive_root=tmp_path)

    written = list((result.run_dir / "tasks").glob("*.json"))
    assert len(written) == 1
    assert written[0].parent == result.run_dir / "tasks"


def test_invalid_dataset_is_rejected_before_any_work(tmp_path, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("must not analyze an invalid dataset")

    monkeypatch.setattr(runner_module, "analyze_module_relevance", explode)
    with pytest.raises(DatasetError):
        evaluate_candidate(baseline(), _dataset(tasks=[]), archive_root=tmp_path)
