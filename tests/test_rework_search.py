"""The outer loop: mutations, search, and the frontier they feed."""

import json

import pytest

from meta_harness.archive_reader import load_run_summary
from meta_harness.frontier import FrontierStore
from meta_harness.rework.benchmark import CaseSet, MatchCase, record_in_frontier, run_benchmark
from meta_harness.rework.matching import Thresholds
from meta_harness.rework.mutations import (
    DEFAULT_SEED,
    builtin_mutations,
    generate_variants,
    resolve_mutations,
    variant_name,
)
from meta_harness.rework.search import (
    FALSE_MATCH_PENALTY,
    SEED_NAME,
    quality,
    search_thresholds,
)


def _issue(issue_id, identifier, title):
    return {"id": issue_id, "identifier": identifier, "title": title,
            "state": {"id": "s", "name": "Todo", "type": "unstarted"}}


ISSUES = [
    _issue("i1", "SIG-1", "6.5 Tabla de Carga por responsable"),
    _issue("i2", "SIG-2", "Mover candidato a Tanda enviada a cliente"),
    _issue("i3", "SIG-3", "Mover candidato a Tanda enviada a Comercial"),
    _issue("i4", "SIG-4", "Notificación por correo al Jefe de selección"),
]


def _cases(cases=None):
    return CaseSet(
        name="search-cases",
        cases=cases or [
            MatchCase("- ✅ *6.5 Tabla de Carga por responsable*", "SIG-1"),
            MatchCase("- ✅ *Mover candidato a Tanda enviada a cliente*", "SIG-2"),
            MatchCase("- ✅ *Notificación*", ""),
        ],
        issues=ISSUES,
    )


# --- mutations ----------------------------------------------------------------


def test_every_mutation_produces_a_valid_setting():
    for mutation in builtin_mutations().values():
        mutation(DEFAULT_SEED).validate()


def test_each_mutation_moves_exactly_one_axis():
    """A variant changing two knobs at once cannot attribute its win."""
    for mutation in builtin_mutations().values():
        variant = mutation(DEFAULT_SEED)
        changed = [
            field
            for field in ("strong_match", "min_candidate", "ambiguity_margin", "max_candidates")
            if getattr(variant, field) != getattr(DEFAULT_SEED, field)
        ]
        assert len(changed) <= 1, f"{mutation.slug} changed {changed}"


def test_looser_match_never_crosses_min_candidate():
    """If strong_match fell below min_candidate the two bars would contradict."""
    tight = Thresholds(strong_match=0.6, min_candidate=0.55)
    variant = builtin_mutations()["looser_match"](tight)
    variant.validate()
    assert variant.strong_match > variant.min_candidate


def test_wider_net_never_crosses_strong_match():
    variant = builtin_mutations()["wider_net"](Thresholds(strong_match=0.4, min_candidate=0.35))
    variant.validate()
    assert variant.min_candidate < variant.strong_match


def test_mutations_are_clamped_at_the_rails():
    extreme = Thresholds(strong_match=0.98, min_candidate=0.15, ambiguity_margin=0.30)
    for mutation in builtin_mutations().values():
        mutation(extreme).validate()


def test_variants_that_change_nothing_are_dropped():
    """A mutation flat against a rail would otherwise put a duplicate of the
    seed on the frontier under a different name."""
    pinned = Thresholds(strong_match=0.98, min_candidate=0.15, ambiguity_margin=0.30)
    names = [name for name, _, _ in generate_variants(pinned)]
    assert "seed-stricter_match" not in names   # already at the ceiling
    assert "seed-cautious" not in names         # already at the margin ceiling


def test_mutation_order_is_stable():
    assert [m.slug for m in resolve_mutations()] == sorted(builtin_mutations())
    assert [m.slug for m in resolve_mutations(["cautious", "cautious"])] == ["cautious"]


def test_unknown_mutation_is_rejected():
    with pytest.raises(ValueError, match="Unknown mutation"):
        resolve_mutations(["does_not_exist"])


def test_variant_names_are_derived_from_the_seed():
    assert variant_name("seed", builtin_mutations()["cautious"]) == "seed-cautious"


# --- ranking ------------------------------------------------------------------


def test_quality_penalises_false_matches(tmp_path):
    """The whole reason the search does not just rank by pass rate."""
    clean = run_benchmark(_cases(), archive_root=tmp_path / "a", run_name="clean")
    assert quality(clean) == clean.pass_rate  # no false matches, no penalty

    reckless = run_benchmark(
        _cases(), archive_root=tmp_path / "b", run_name="reckless",
        thresholds=Thresholds(strong_match=0.30, min_candidate=0.05, ambiguity_margin=0.0),
    )
    assert reckless.false_matches
    assert quality(reckless) < reckless.pass_rate


def test_a_setting_that_trades_misses_for_false_matches_loses(tmp_path):
    strict = run_benchmark(_cases(), archive_root=tmp_path / "s", run_name="strict",
                           thresholds=Thresholds(strong_match=0.95))
    loose = run_benchmark(_cases(), archive_root=tmp_path / "l", run_name="loose",
                          thresholds=Thresholds(strong_match=0.30, min_candidate=0.05,
                                                ambiguity_margin=0.0))
    # Loose resolves more, but resolves some of them wrongly.
    assert len(loose.false_matches) > len(strict.false_matches)
    assert quality(loose) < quality(strict)


def test_penalty_is_explicit():
    assert FALSE_MATCH_PENALTY == 2.0


# --- search -------------------------------------------------------------------


def test_search_scores_the_seed_and_every_variant(tmp_path):
    result = search_thresholds(_cases(), archive_root=tmp_path)
    assert result.seed.name == SEED_NAME
    assert len(result.trials) == len(generate_variants(DEFAULT_SEED))
    assert len(result.ranked) == len(result.trials) + 1


def test_search_writes_a_run_dir_per_trial(tmp_path):
    result = search_thresholds(_cases(), archive_root=tmp_path)
    for trial in result.ranked:
        assert (trial.result.run_dir / "summary.json").exists()
        # Readable by the untouched Hermes reader.
        assert load_run_summary(trial.result.run_dir).benchmark_name == "rework-name-matching"


def test_search_is_deterministic(tmp_path):
    a = search_thresholds(_cases(), archive_root=tmp_path / "a")
    b = search_thresholds(_cases(), archive_root=tmp_path / "b")
    assert [t.name for t in a.ranked] == [t.name for t in b.ranked]
    assert a.best.name == b.best.name


def test_a_tie_keeps_the_seed(tmp_path):
    """Changing thresholds has a cost; ties must not churn them."""
    result = search_thresholds(_cases(), archive_root=tmp_path)
    if not result.improved:
        assert result.best.name == SEED_NAME


def test_search_can_be_limited_to_named_mutations(tmp_path):
    result = search_thresholds(_cases(), archive_root=tmp_path, mutations=["cautious"])
    assert [t.mutation_slug for t in result.trials] == ["cautious"]


def test_search_rejects_an_unknown_mutation(tmp_path):
    with pytest.raises(ValueError, match="Unknown mutation"):
        search_thresholds(_cases(), archive_root=tmp_path, mutations=["nope"])


def test_search_reports_regressions(tmp_path):
    result = search_thresholds(_cases(), archive_root=tmp_path)
    for trial in result.regressions():
        assert trial.quality < result.seed.quality


def test_search_result_serialises(tmp_path):
    payload = search_thresholds(_cases(), archive_root=tmp_path).to_dict()
    assert payload["false_match_penalty"] == FALSE_MATCH_PENALTY
    assert payload["best"]["thresholds"]["strong_match"]
    json.dumps(payload)  # must be JSON-safe


def test_search_reports_progress(tmp_path):
    steps = []
    search_thresholds(_cases(), archive_root=tmp_path, on_step=steps.append)
    assert any("seed" in s for s in steps)


# --- frontier -----------------------------------------------------------------


def test_a_scored_run_lands_on_the_frontier(tmp_path):
    frontier = tmp_path / "frontier.json"
    result = run_benchmark(_cases(), archive_root=tmp_path / "runs", candidate_name="default")
    record_in_frontier(result, frontier)

    entries = FrontierStore(frontier).load()
    assert len(entries) == 1
    assert entries[0].candidate_name == "default"
    assert entries[0].benchmark_name == "rework-name-matching"


def test_the_frontier_entry_carries_the_false_match_count(tmp_path):
    """Pass rate alone would let a dangerous setting sit at the top unmarked."""
    frontier = tmp_path / "frontier.json"
    reckless = run_benchmark(
        _cases(), archive_root=tmp_path / "runs", candidate_name="reckless",
        thresholds=Thresholds(strong_match=0.30, min_candidate=0.05, ambiguity_margin=0.0),
    )
    record_in_frontier(reckless, frontier)
    assert "false_matches=" in FrontierStore(frontier).load()[0].notes


def test_search_records_every_trial_on_the_frontier(tmp_path):
    frontier = tmp_path / "frontier.json"
    result = search_thresholds(_cases(), archive_root=tmp_path / "runs", frontier_path=frontier)
    assert len(FrontierStore(frontier).load()) == len(result.ranked)


def test_the_frontier_ranks_by_pass_rate(tmp_path):
    """Hermes's own rule, left untouched — which is exactly why the search
    reports its own winner separately."""
    frontier = tmp_path / "frontier.json"
    search_thresholds(_cases(), archive_root=tmp_path / "runs", frontier_path=frontier)
    top = FrontierStore(frontier).top_for_benchmark("rework-name-matching", limit=99)
    assert [e.pass_rate for e in top] == sorted((e.pass_rate for e in top), reverse=True)
