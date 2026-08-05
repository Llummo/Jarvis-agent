"""Scoring a plan against the app that exists, before running it.

Every plan the harness has produced failed in one of three ways: it named
something that is not there, it used a field without opening the view holding
it, or it filled a form and never submitted. All three are visible from the flow
map, so they are caught here rather than in a report that blames the product.
"""

from __future__ import annotations

import json

from meta_harness.qa.flow import FlowMap, View
from meta_harness.qa.plan import Expectation, TestCase, TestPlan, TestStep
from meta_harness.qa.plan_bench import (
    choose_best,
    judge_case,
    score_plan,
    write_run_dir,
)
from meta_harness.qa.vocabulary import Vocabulary

LISTING = View(
    name="PERSONAS",
    path=[],
    vocabulary=Vocabulary(
        actions=["Agregar persona", "Importar CSV"],
        fields=["Buscar personas"],
        texts=["PERSONAS"],
    ),
)
FORM = View(
    name="AGREGAR PERSONA",
    path=["Agregar persona"],
    vocabulary=Vocabulary(
        actions=["Cancelar", "Agregar persona"],
        form_actions=["Agregar persona"],
        fields=["Nombres *", "Apellido paterno *", "Teléfono *"],
        texts=["AGREGAR PERSONA"],
    ),
)
FLOW = FlowMap(route="/erp/talent/people", views=[LISTING, FORM])

WRITES = Expectation(ui_text="PERSONAS", api_path="/api/v1/talent/people", api_method="POST")


def _case(name, steps, expectation=WRITES):
    return TestCase(name=name, criterion=name, steps=steps, expectation=expectation)


def test_a_plan_that_follows_the_flow_passes():
    case = _case(
        "01-alta",
        [
            TestStep("goto", "/erp/talent/people"),
            TestStep("click_text", "Agregar persona"),
            TestStep("type_label", "Nombres *", "Prueba"),
            TestStep("click_text", "Agregar persona"),
        ],
    )
    verdict = judge_case(case, FLOW)
    assert verdict.passed, verdict.why()


def test_a_step_naming_something_absent_is_ungrounded():
    """«Guardar» no existe: el plan fallará y culpará a la app."""
    case = _case(
        "01-alta",
        [
            TestStep("goto", "/erp/talent/people"),
            TestStep("click_text", "Agregar persona"),
            TestStep("click_text", "Guardar"),
        ],
    )
    verdict = judge_case(case, FLOW)
    assert not verdict.passed
    assert len(verdict.ungrounded) == 1
    assert "no existe en el flujo" in verdict.why()


def test_using_a_field_without_opening_its_view_is_unreachable():
    """El campo existe, pero el plan nunca pulsa lo que lo muestra."""
    case = _case(
        "01-alta",
        [
            TestStep("goto", "/erp/talent/people"),
            TestStep("type_label", "Nombres *", "Prueba"),
            TestStep("click_text", "Agregar persona"),
        ],
    )
    verdict = judge_case(case, FLOW)
    assert not verdict.passed
    assert len(verdict.unreachable) == 1
    assert "que el plan no abre" in verdict.why()


def test_a_writing_criterion_that_never_submits_does_not_close():
    case = _case(
        "01-alta",
        [
            TestStep("goto", "/erp/talent/people"),
            TestStep("click_text", "Agregar persona"),
            TestStep("type_label", "Nombres *", "Prueba"),
        ],
    )
    verdict = judge_case(case, FLOW)
    assert not verdict.passed
    assert "nunca pulsa nada que envíe" in verdict.why()


def test_a_read_only_criterion_is_not_asked_to_submit():
    """Un criterio visual no escribe, así que no se le exige confirmar."""
    case = _case(
        "01-paridad",
        [
            TestStep("goto", "/erp/talent/people"),
            TestStep("click_text", "Agregar persona"),
            TestStep("wait_text", "Nombres *"),
        ],
        expectation=Expectation(ui_text="Nombres *", reads_only=True),
    )
    assert judge_case(case, FLOW).passed


def test_the_best_candidate_is_the_one_that_invents_least_at_equal_pass_rate():
    """Inventar un botón cuesta más que desordenar un clic: falla más lejos."""
    inventing = _case("01", [TestStep("click_text", "Guardar")])
    misordered = _case("01", [TestStep("type_label", "Nombres *", "x")])

    worse = score_plan(
        TestPlan(ticket_id="SIG-1", route="/erp/talent/people", cases=[inventing]),
        FLOW, candidate_name="inventa",
    )
    better = score_plan(
        TestPlan(ticket_id="SIG-1", route="/erp/talent/people", cases=[misordered]),
        FLOW, candidate_name="desordena",
    )
    assert worse.pass_rate == better.pass_rate == 0.0
    assert choose_best([worse, better]) is better


def test_the_run_is_written_where_the_outer_loop_reads_runs(tmp_path):
    """Mismo formato que el resto de Hermes, o show-frontier no lo ve."""
    result = score_plan(
        TestPlan(
            ticket_id="SIG-109",
            route="/erp/talent/people",
            cases=[
                _case(
                    "01-alta",
                    [
                        TestStep("goto", "/erp/talent/people"),
                        TestStep("click_text", "Agregar persona"),
                        TestStep("type_label", "Nombres *", "Prueba"),
                        TestStep("click_text", "Agregar persona"),
                    ],
                )
            ],
        ),
        FLOW,
        candidate_name="directo",
    )
    run_dir = write_run_dir(result, tmp_path, FLOW)

    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["eval_metrics"]["eval/pass_rate"] == 1.0
    # Las dos formas de fallar viajan aparte del pass rate, como en el
    # benchmark del matcher: una sola cifra las confundiría.
    assert summary["eval_metrics"]["eval/ungrounded_steps"] == 0
    assert summary["eval_metrics"]["eval/unreachable_steps"] == 0

    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    runner = manifest["outer_loop"]["benchmark_runner"]
    assert runner["total_tasks"] == 1
    # Sin esto dos candidatos no se pueden comparar aunque midan lo mismo.
    assert runner["task_selection"]
    assert (run_dir / "tasks" / "01-alta.json").is_file()


def test_scoring_without_a_map_is_not_attempted():
    """Sin vistas no hay contra qué medir, y un 0% sería una mentira."""
    empty = FlowMap(route="/x")
    result = score_plan(
        TestPlan(ticket_id="SIG-1", route="/x", cases=[_case("01", [TestStep("goto", "/x")])]),
        empty, candidate_name="sin-mapa",
    )
    # Un plan de solo `goto` no nombra nada, así que no hay paso que anclar.
    assert result.ungrounded == 0
