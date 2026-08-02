import pytest

from meta_harness.rework import commands as commands_module
from meta_harness.rework import queries as queries_module
from meta_harness.rework.commands import execute_rework_plan
from meta_harness.rework.matching import (
    STATUS_AMBIGUOUS,
    STATUS_MATCHED,
    STATUS_UNMATCHED,
    IssueMatch,
    ParsedName,
    parse_line,
    parse_pasted_names,
    resolve_names,
    score_title,
)
from meta_harness.rework.models import ReworkItem, ReworkPlan, ReworkReport, ReworkOutcome
from meta_harness.rework.queries import find_cancelled_state, preview_rework
from meta_harness.rework.reasoning import _parse_choice
from meta_harness.ticket_generator import ProposedTicket
from meta_harness.ticket_reformat import ReformattedTicket, TicketReformatError


def _issue(issue_id, identifier, title, state_type="unstarted", state_name="Todo"):
    return {
        "id": issue_id,
        "identifier": identifier,
        "title": title,
        "state": {"id": f"st-{state_type}", "name": state_name, "type": state_type},
    }


PASTED = """
- ✅ *6.5 Tabla de Carga por responsable* — Pertenece al módulo (93%) *check*
- ✅ *[SIG-60] Fase 1: Opening — campos ClientPrice* — Pertenece al módulo (86%) *check*
-
- ✅ *1.16. Candidato que se retira del proceso* — Pertenece al módulo (88%) check
"""


# --- parsing ------------------------------------------------------------------


def test_parses_a_whatsapp_style_list():
    names = parse_pasted_names(PASTED)
    assert [n.title for n in names] == [
        "6.5 Tabla de Carga por responsable",
        "Fase 1: Opening — campos ClientPrice",
        "1.16. Candidato que se retira del proceso",
    ]


def test_blank_and_decorative_lines_are_skipped():
    assert parse_pasted_names("\n- \n  \n*  *\n") == []


def test_identifier_is_extracted_and_removed_from_the_title():
    parsed = parse_line("- ✅ *[SIG-60] Fase 1: Opening*")
    assert parsed.identifier == "SIG-60"
    assert parsed.title == "Fase 1: Opening"


def test_numeric_prefix_is_extracted():
    assert parse_line("6.5 Tabla de Carga").numeric_prefix == "6.5"
    assert parse_line("1.16. Candidato").numeric_prefix == "1.16"
    assert parse_line("Notificación").numeric_prefix == ""


def test_verdict_tail_is_stripped_but_a_real_em_dash_survives():
    # The tail is only cut when it actually looks like a relevance verdict.
    kept = parse_line('Renombrar "Opening" — Requerimiento en URLs')
    assert kept.title == 'Renombrar "Opening" — Requerimiento en URLs'
    cut = parse_line("Renombrar Opening — Pertenece al módulo (85%) *check*")
    assert cut.title == "Renombrar Opening"


def test_trailing_commentary_after_the_verdict_is_dropped():
    parsed = parse_line(
        "- ✅ *1.1. Agregar candidatos* — Pertenece al módulo (72%) *check* "
        "de hecho este ticket está sin resolver"
    )
    assert parsed.title == "1.1. Agregar candidatos"


# --- scoring ------------------------------------------------------------------


def test_identical_titles_score_one():
    assert score_title(ParsedName("x", "Tabla de Carga"), "Tabla de Carga") == 1.0


def test_accents_and_case_do_not_matter():
    assert score_title(ParsedName("x", "Calculo de Days Open"), "Cálculo de days open") == 1.0


def test_matching_numeric_prefix_lifts_the_score():
    parsed = ParsedName("x", "6.5 Tabla de Carga", numeric_prefix="6.5")
    with_prefix = score_title(parsed, "6.5 Tabla de Carga por responsable")
    without = score_title(ParsedName("x", "Tabla de Carga"), "Tabla de Carga por responsable")
    assert with_prefix > without


def test_conflicting_numeric_prefix_is_penalised_hard():
    """6.1 and 6.5 are different tickets however similar the words are."""
    parsed = ParsedName("x", "6.5 Tabla de Carga", numeric_prefix="6.5")
    assert score_title(parsed, "6.1 Tabla de Carga") < 0.5


def test_a_shortened_name_still_matches_the_longer_real_title():
    parsed = ParsedName("x", "Tabla de Carga")
    assert score_title(parsed, "Tabla de Carga por responsable del proceso") >= 0.6


def test_unrelated_titles_score_low():
    assert score_title(ParsedName("x", "Catálogo de Reject"), "Notificación por correo") < 0.35


# --- resolution ---------------------------------------------------------------


def test_identifier_wins_outright():
    issues = [_issue("i1", "SIG-60", "Something else entirely")]
    parsed = [ParsedName("raw", "Totally different words", identifier="SIG-60")]
    resolution = resolve_names(parsed, issues)[0]
    assert resolution.status == STATUS_MATCHED
    assert resolution.method == "identifier"
    assert resolution.chosen.issue_id == "i1"


def test_a_clear_title_match_resolves_without_reasoning():
    issues = [_issue("i1", "SIG-1", "6.5 Tabla de Carga por responsable"), _issue("i2", "SIG-2", "Notificación")]
    resolution = resolve_names(parse_pasted_names("6.5 Tabla de Carga por responsable"), issues)[0]
    assert resolution.status == STATUS_MATCHED
    assert resolution.method == "score"
    assert resolution.chosen.issue_id == "i1"


def test_no_plausible_issue_is_reported_unmatched():
    issues = [_issue("i1", "SIG-1", "Completely unrelated subject")]
    resolution = resolve_names(parse_pasted_names("Catálogo de Reject"), issues)[0]
    assert resolution.status == STATUS_UNMATCHED
    assert resolution.chosen is None


def test_indistinguishable_candidates_are_ambiguous_not_guessed():
    issues = [
        _issue("i1", "SIG-1", "Mover candidato a Tanda enviada a cliente"),
        _issue("i2", "SIG-2", "Mover candidato a Tanda enviada a Comercial"),
    ]
    resolution = resolve_names(parse_pasted_names("Mover candidato a Tanda enviada"), issues)[0]
    assert resolution.status == STATUS_AMBIGUOUS
    assert resolution.chosen is None
    assert len(resolution.candidates) == 2


def test_a_reasoner_can_settle_an_ambiguous_name():
    issues = [
        _issue("i1", "SIG-1", "Mover candidato a Tanda enviada a cliente"),
        _issue("i2", "SIG-2", "Mover candidato a Tanda enviada a Comercial"),
    ]
    resolution = resolve_names(
        parse_pasted_names("Mover candidato a Tanda enviada"), issues, reasoner=lambda p, c: "i2"
    )[0]
    assert resolution.status == STATUS_MATCHED
    assert resolution.method == "reasoning"
    assert resolution.chosen.issue_id == "i2"


def test_a_reasoner_that_declines_leaves_the_name_ambiguous():
    issues = [
        _issue("i1", "SIG-1", "Mover candidato a Tanda enviada a cliente"),
        _issue("i2", "SIG-2", "Mover candidato a Tanda enviada a Comercial"),
    ]
    resolution = resolve_names(
        parse_pasted_names("Mover candidato a Tanda enviada"), issues, reasoner=lambda p, c: None
    )[0]
    assert resolution.status == STATUS_AMBIGUOUS


def test_a_reasoner_cannot_invent_an_issue_id():
    issues = [_issue("i1", "SIG-1", "Mover candidato a Tanda enviada a cliente"),
              _issue("i2", "SIG-2", "Mover candidato a Tanda enviada a Comercial")]
    resolution = resolve_names(
        parse_pasted_names("Mover candidato a Tanda enviada"), issues, reasoner=lambda p, c: "does-not-exist"
    )[0]
    assert resolution.status == STATUS_AMBIGUOUS


def test_resolution_survives_an_empty_issue_list():
    assert resolve_names(parse_pasted_names("anything at all"), [])[0].status == STATUS_UNMATCHED


def test_grep_narrowing_does_not_lose_a_match(monkeypatch):
    """ripgrep is an accelerator; a miss must fall back to the full scan."""
    monkeypatch.setattr("meta_harness.rework.matching.grep_narrow", lambda *a, **k: [])
    issues = [_issue("i1", "SIG-1", "6.5 Tabla de Carga por responsable")]
    resolution = resolve_names(parse_pasted_names("6.5 Tabla de Carga por responsable"), issues)[0]
    assert resolution.status == STATUS_MATCHED


# --- plan validation ----------------------------------------------------------


def _plan(**overrides):
    payload = {
        "team_id": "team-1",
        "parent_issue_id": "parent-1",
        "items": [ReworkItem("i1", "SIG-1", "Old title")],
        "cancelled_state_id": "state-cancelled",
    }
    payload.update(overrides)
    return ReworkPlan(**payload)


def test_plan_requires_a_parent():
    with pytest.raises(ValueError, match="parent"):
        _plan(parent_issue_id="").validate()


def test_plan_requires_items():
    with pytest.raises(ValueError, match="no issues"):
        _plan(items=[]).validate()


def test_plan_rejects_the_parent_being_reworked():
    with pytest.raises(ValueError, match="cannot also be"):
        _plan(items=[ReworkItem("parent-1", "SIG-P", "Parent")]).validate()


def test_plan_rejects_a_duplicate_issue():
    items = [ReworkItem("i1", "SIG-1", "a"), ReworkItem("i1", "SIG-1", "a")]
    with pytest.raises(ValueError, match="twice"):
        _plan(items=items).validate()


def test_plan_requires_a_cancelled_state_when_cancelling():
    with pytest.raises(ValueError, match="cancelled state"):
        _plan(cancelled_state_id="").validate()


def test_plan_without_cancelling_needs_no_cancelled_state():
    _plan(cancelled_state_id="", cancel_originals=False).validate()


# --- execution ----------------------------------------------------------------


def _reformatted(ticket_id, title="Reworked title"):
    return ReformattedTicket(
        ticket_id=ticket_id,
        tracker="linear",
        original_title="Old title",
        original_description="old body",
        ticket=ProposedTicket(title=title, description="body"),
        formatted_title=title,
        formatted_description="formatted body",
    )


@pytest.fixture
def linear(monkeypatch):
    """Records every write, so ordering can be asserted."""
    calls = []

    monkeypatch.setattr(
        commands_module, "reformat_ticket",
        lambda tid, **k: calls.append(("reformat", tid)) or _reformatted(tid),
    )
    monkeypatch.setattr(
        commands_module, "create_linear_issue",
        lambda team, title, desc, **k: calls.append(("create", k.get("parent_id"))) or f"new-{len(calls)}",
    )
    monkeypatch.setattr(
        commands_module, "update_linear_issue_state",
        lambda iid, sid: calls.append(("cancel", iid)) or {},
    )
    return calls


def test_creates_the_replacement_before_cancelling_the_original(linear):
    execute_rework_plan(_plan())
    assert [step for step, _ in linear] == ["reformat", "create", "cancel"]


def test_new_issue_is_attached_to_the_chosen_parent(linear):
    execute_rework_plan(_plan())
    create = next(payload for step, payload in linear if step == "create")
    assert create == "parent-1"


def test_original_is_moved_to_cancelled(linear):
    execute_rework_plan(_plan())
    assert ("cancel", "i1") in linear


def test_report_counts_a_successful_rework(linear):
    report = execute_rework_plan(_plan())
    assert len(report.created) == 1
    assert report.outcomes[0].ok
    assert report.outcomes[0].cancelled


def test_cancelling_can_be_turned_off(linear):
    report = execute_rework_plan(_plan(cancel_originals=False))
    assert [step for step, _ in linear] == ["reformat", "create"]
    assert report.outcomes[0].cancelled is False


def test_a_failed_reformat_leaves_the_original_untouched(monkeypatch):
    def boom(*args, **kwargs):
        raise TicketReformatError("unparseable")

    written = []
    monkeypatch.setattr(commands_module, "reformat_ticket", boom)
    monkeypatch.setattr(commands_module, "create_linear_issue", lambda *a, **k: written.append("create"))
    monkeypatch.setattr(commands_module, "update_linear_issue_state", lambda *a: written.append("cancel"))

    report = execute_rework_plan(_plan())
    assert written == []
    assert report.failed
    assert "reformat failed" in report.outcomes[0].error


def test_a_failed_create_never_cancels_the_original(monkeypatch):
    cancelled = []
    monkeypatch.setattr(commands_module, "reformat_ticket", lambda tid, **k: _reformatted(tid))
    monkeypatch.setattr(
        commands_module, "create_linear_issue",
        lambda *a, **k: (_ for _ in ()).throw(commands_module.LinearIssueError("rejected")),
    )
    monkeypatch.setattr(commands_module, "update_linear_issue_state", lambda *a: cancelled.append(a))

    report = execute_rework_plan(_plan())
    assert cancelled == []
    assert "create failed" in report.outcomes[0].error


def test_a_failed_cancel_reports_the_original_as_still_open(monkeypatch):
    monkeypatch.setattr(commands_module, "reformat_ticket", lambda tid, **k: _reformatted(tid))
    monkeypatch.setattr(commands_module, "create_linear_issue", lambda *a, **k: "new-1")
    monkeypatch.setattr(
        commands_module, "update_linear_issue_state",
        lambda *a: (_ for _ in ()).throw(commands_module.LinearIssueError("no permission")),
    )

    report = execute_rework_plan(_plan())
    outcome = report.outcomes[0]
    # The replacement exists, so this is not a failure — but a human must finish it.
    assert outcome.created_issue_id == "new-1"
    assert outcome.needs_attention
    assert not outcome.error
    assert report.stranded


def test_one_bad_issue_does_not_stop_the_batch(monkeypatch):
    def flaky(ticket_id, **kwargs):
        if ticket_id == "i2":
            raise TicketReformatError("bad")
        return _reformatted(ticket_id)

    monkeypatch.setattr(commands_module, "reformat_ticket", flaky)
    monkeypatch.setattr(commands_module, "create_linear_issue", lambda *a, **k: "new")
    monkeypatch.setattr(commands_module, "update_linear_issue_state", lambda *a: {})

    plan = _plan(items=[ReworkItem(f"i{i}", f"SIG-{i}", "t") for i in (1, 2, 3)])
    report = execute_rework_plan(plan)
    assert len(report.created) == 2
    assert len(report.failed) == 1


def test_progress_is_reported_per_issue(linear):
    steps = []
    execute_rework_plan(_plan(), on_step=steps.append)
    assert any("1/1" in step for step in steps)
    assert any("Finished" in step for step in steps)


# --- queries ------------------------------------------------------------------


def test_cancelled_state_is_found_by_type(monkeypatch):
    monkeypatch.setattr(
        queries_module, "list_linear_states",
        lambda t: [{"id": "s1", "name": "Done", "type": "completed"},
                   {"id": "s2", "name": "Descartado", "type": "canceled"}],
    )
    assert find_cancelled_state("team").id == "s2"


def test_cancelled_state_falls_back_to_the_name(monkeypatch):
    """A workflow customized past recognition still has a cancelled column."""
    monkeypatch.setattr(
        queries_module, "list_linear_states",
        lambda t: [{"id": "s1", "name": "Done", "type": "completed"},
                   {"id": "s2", "name": "Cancelados", "type": "unstarted"}],
    )
    assert find_cancelled_state("team").id == "s2"


def test_missing_cancelled_state_is_reported_not_guessed(monkeypatch):
    monkeypatch.setattr(
        queries_module, "list_linear_states",
        lambda t: [{"id": "s1", "name": "Done", "type": "completed"}],
    )
    assert find_cancelled_state("team") is None


def _states(monkeypatch, states=None):
    monkeypatch.setattr(
        queries_module, "list_linear_states",
        lambda t: states if states is not None else [{"id": "sc", "name": "Cancelled", "type": "canceled"}],
    )


def test_preview_resolves_and_reports_the_cancelled_state(monkeypatch):
    _states(monkeypatch)
    issues = [_issue("i1", "SIG-1", "6.5 Tabla de Carga por responsable")]
    preview = preview_rework("team", "6.5 Tabla de Carga por responsable", issues=issues)
    assert preview.cancelled_state.id == "sc"
    assert len(preview.matched) == 1
    assert preview.unresolved == []


def test_preview_warns_when_a_name_cannot_be_matched(monkeypatch):
    _states(monkeypatch)
    preview = preview_rework("team", "Something nobody has", issues=[_issue("i1", "SIG-1", "Other")])
    assert preview.unresolved
    assert any("could not be matched" in w for w in preview.warnings)


def test_preview_warns_when_two_names_hit_the_same_issue(monkeypatch):
    _states(monkeypatch)
    issues = [_issue("i1", "SIG-1", "Tabla de Carga por responsable")]
    preview = preview_rework(
        "team", "Tabla de Carga por responsable\nTabla de Carga por responsable", issues=issues
    )
    assert any("same issue" in w for w in preview.warnings)


def test_preview_warns_when_the_team_has_no_cancelled_state(monkeypatch):
    _states(monkeypatch, [{"id": "s1", "name": "Done", "type": "completed"}])
    preview = preview_rework("team", "Tabla", issues=[_issue("i1", "SIG-1", "Tabla")])
    assert preview.cancelled_state is None
    assert any("no cancelled state" in w for w in preview.warnings)


def test_preview_warns_on_empty_input(monkeypatch):
    _states(monkeypatch)
    preview = preview_rework("team", "   \n  ", issues=[])
    assert any("No ticket names" in w for w in preview.warnings)


def test_preview_is_serializable(monkeypatch):
    _states(monkeypatch)
    issues = [_issue("i1", "SIG-1", "Tabla de Carga por responsable")]
    payload = preview_rework("team", "Tabla de Carga por responsable", issues=issues).to_dict()
    assert payload["matched_count"] == 1
    assert payload["resolutions"][0]["chosen"]["identifier"] == "SIG-1"


# --- reasoning parsing --------------------------------------------------------


def test_reasoner_output_must_name_an_offered_candidate():
    assert _parse_choice('{"issue_id": "i9"}', ["i1", "i2"]) is None
    assert _parse_choice('{"issue_id": "i2"}', ["i1", "i2"]) == "i2"


def test_reasoner_declining_is_respected():
    assert _parse_choice('{"issue_id": null, "why": "none match"}', ["i1"]) is None


def test_low_confidence_is_treated_as_declining():
    assert _parse_choice('{"issue_id": "i1", "confidence": 0.2}', ["i1"]) is None
    assert _parse_choice('{"issue_id": "i1", "confidence": 0.9}', ["i1"]) == "i1"


def test_reasoner_output_wrapped_in_prose_is_still_parsed():
    raw = 'Looking at these, I think:\n{"issue_id": "i1", "confidence": 0.8}\nHope that helps.'
    assert _parse_choice(raw, ["i1"]) == "i1"


def test_unparseable_reasoner_output_declines():
    assert _parse_choice("no json here", ["i1"]) is None
    assert _parse_choice("", ["i1"]) is None


# --- report -------------------------------------------------------------------


def test_report_summary_mentions_stranded_originals():
    report = ReworkReport(
        parent_issue_id="p",
        outcomes=[ReworkOutcome("i1", "SIG-1", "t", created_issue_id="n1", cancelled=False)],
    )
    assert "still open" in report.summary_line()


# --- matcher benchmark --------------------------------------------------------

from meta_harness.archive_reader import load_run_summary  # noqa: E402
from meta_harness.comparison import build_comparison_report  # noqa: E402
from meta_harness.rework.benchmark import (  # noqa: E402
    OUTCOME_CORRECT,
    OUTCOME_FALSE_MATCH,
    OUTCOME_MISS,
    BenchmarkError,
    CaseSet,
    MatchCase,
    run_benchmark,
    sweep,
)
from meta_harness.rework.matching import Thresholds  # noqa: E402


BENCH_ISSUES = [
    _issue("i1", "SIG-1", "6.5 Tabla de Carga por responsable"),
    _issue("i2", "SIG-2", "Mover candidato a Tanda enviada a cliente"),
    _issue("i3", "SIG-3", "Mover candidato a Tanda enviada a Comercial"),
]


def _case_set(cases=None):
    return CaseSet(
        name="sigo-selection",
        cases=cases
        if cases is not None
        else [
            MatchCase("- ✅ *6.5 Tabla de Carga por responsable* — Pertenece al módulo (93%)", "SIG-1"),
            MatchCase("Algo que no existe en absoluto", ""),
        ],
        issues=BENCH_ISSUES,
    )


def test_thresholds_reject_an_impossible_configuration():
    with pytest.raises(ValueError, match="min_candidate"):
        Thresholds(strong_match=0.5, min_candidate=0.9).validate()


def test_thresholds_change_what_resolves():
    """The knob has to actually move the outcome, or measuring it is pointless.

    "Tabla" scores 0.775 against the real title — above min_candidate, below
    the default strong_match, so it is exactly the case the threshold decides.
    """
    names = parse_pasted_names("Tabla")
    only = [_issue("i1", "SIG-1", "6.5 Tabla de Carga por responsable")]

    assert resolve_names(names, only)[0].status == STATUS_AMBIGUOUS
    loosened = resolve_names(names, only, thresholds=Thresholds(strong_match=0.7))[0]
    assert loosened.status == STATUS_MATCHED
    assert loosened.chosen.identifier == "SIG-1"


def test_raising_the_bar_makes_a_weak_match_ambiguous():
    """The safety direction: a stricter threshold must refuse more, not less."""
    names = parse_pasted_names("Tabla de Carga")  # scores 0.85
    only = [_issue("i1", "SIG-1", "6.5 Tabla de Carga por responsable")]

    assert resolve_names(names, only)[0].status == STATUS_MATCHED
    stricter = resolve_names(names, only, thresholds=Thresholds(strong_match=0.9))[0]
    assert stricter.status == STATUS_AMBIGUOUS


def test_case_set_rejects_an_unreachable_expectation():
    bad = _case_set([MatchCase("6.5 Tabla de Carga por responsable", "SIG-999")])
    with pytest.raises(BenchmarkError, match="not in the issue list"):
        bad.validate()


def test_case_set_rejects_two_cases_with_the_same_name():
    duplicated = _case_set([MatchCase("6.5 Tabla", "SIG-1"), MatchCase("6.5 Tabla", "SIG-1")])
    with pytest.raises(BenchmarkError, match="same name"):
        duplicated.validate()


def test_case_set_round_trips(tmp_path):
    original = _case_set()
    path = original.save(tmp_path / "cases.json")
    assert CaseSet.load(path).to_dict() == original.to_dict()


def test_benchmark_scores_a_correct_match(tmp_path):
    result = run_benchmark(_case_set(), archive_root=tmp_path)
    assert result.pass_rate == 1.0
    assert [o.outcome for o in result.outcomes] == [OUTCOME_CORRECT, OUTCOME_CORRECT]


def test_benchmark_counts_a_wrong_match_as_a_false_match(tmp_path):
    cases = [MatchCase("Mover candidato a Tanda enviada a cliente", "SIG-3")]
    result = run_benchmark(_case_set(cases), archive_root=tmp_path)
    assert result.false_matches
    assert not result.misses


def test_benchmark_counts_a_missed_name_separately(tmp_path):
    cases = [MatchCase("Mover candidato a Tanda enviada", "SIG-2")]
    result = run_benchmark(_case_set(cases), archive_root=tmp_path)
    # Ambiguous, so nothing was chosen — a miss, not a false match.
    assert result.misses
    assert not result.false_matches


def test_a_name_expected_to_match_nothing_that_matches_is_a_false_match(tmp_path):
    cases = [MatchCase("6.5 Tabla de Carga por responsable", "")]
    result = run_benchmark(_case_set(cases), archive_root=tmp_path)
    assert result.false_matches


def test_benchmark_run_dir_is_read_by_the_hermes_archive_reader(tmp_path):
    result = run_benchmark(_case_set(), archive_root=tmp_path)
    summary = load_run_summary(result.run_dir)
    assert summary.benchmark_name == "rework-name-matching"
    assert summary.eval_metrics["eval/total_tasks"] == 2
    assert len(summary.task_results) == 2


def test_false_matches_are_reported_apart_from_pass_rate(tmp_path):
    cases = [MatchCase("Mover candidato a Tanda enviada a cliente", "SIG-3")]
    result = run_benchmark(_case_set(cases), archive_root=tmp_path)
    metrics = load_run_summary(result.run_dir).eval_metrics
    assert metrics["eval/false_matches"] == 1
    assert metrics["eval/misses"] == 0


def test_two_threshold_settings_are_comparable_by_hermes(tmp_path):
    """The payoff: pick thresholds with compare-runs instead of by feel."""
    results = sweep(
        _case_set(),
        archive_root=tmp_path,
        candidates=[
            ("strict", Thresholds()),
            ("loose", Thresholds(strong_match=0.4, ambiguity_margin=0.0)),
        ],
    )
    report = build_comparison_report(
        load_run_summary(results[0].run_dir), load_run_summary(results[1].run_dir)
    )
    assert report.task_selection_status == "matching"
    assert report.total_tasks == 2


def test_benchmark_does_not_use_the_model(tmp_path, monkeypatch):
    """A non-deterministic benchmark cannot attribute a change to a threshold."""
    import meta_harness.rework.reasoning as reasoning_module

    monkeypatch.setattr(
        reasoning_module, "_find_claude",
        lambda: (_ for _ in ()).throw(AssertionError("the benchmark must not call Claude")),
    )
    run_benchmark(_case_set(), archive_root=tmp_path)


# --- CQRS boundary ------------------------------------------------------------


def test_the_query_side_cannot_write():
    """The read/write split is the reason preview is safe to re-run.

    Enforced rather than documented: an import is the easiest way for a write
    to end up on the read path, and nothing else would catch it.
    """
    import ast
    from pathlib import Path

    writes = {
        "create_linear_issue",
        "update_linear_issue",
        "update_linear_issue_state",
        "create_clickup_task",
        "update_clickup_task",
        "execute_rework_plan",
        "apply_reformatted_ticket",
    }
    source = Path(queries_module.__file__).read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    leaked = imported & writes
    assert not leaked, f"queries.py must not import write operations: {sorted(leaked)}"


def test_the_command_side_is_the_only_writer():
    """Every Linear write in the package goes through commands.py."""
    import ast
    from pathlib import Path

    package = Path(commands_module.__file__).parent
    writers = {"create_linear_issue", "update_linear_issue_state", "update_linear_issue"}
    offenders = []
    for path in sorted(package.glob("*.py")):
        if path.name == "commands.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and any(a.name in writers for a in node.names):
                offenders.append(path.name)
    assert not offenders, f"only commands.py may import Linear writes; found in {offenders}"
