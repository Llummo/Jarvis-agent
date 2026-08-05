"""Reading a ticket into testable facts, and turning criteria into steps."""

import json

import pytest

from meta_harness.qa import authoring as authoring_module
from meta_harness.qa.authoring import AuthoringError, author_plan, parse_steps
from meta_harness.qa.ticket_shape import parse_ticket

HOUSE = """📄 USER STORY: GESTIÓN DE POOL DE TALENTO

Título: Formulario de alta de persona

📍 Ruta / Vista UI: /erp/talent/people

🔌 Endpoint Backend: POST /api/v1/talent/people

📝 DESCRIPCIÓN
Como reclutador, quiero registrar personas.

✅ CRITERIOS DE ACEPTACIÓN

📌 Criterio 1: Alta correcta

* Dado que el formulario es válido,
* cuando se confirme,
* entonces la persona queda registrada.

📌 Criterio 2: Campos obligatorios

* Dado que falta el nombre,
* cuando se confirme,
* entonces se muestra un error.
"""


# --- reading the ticket -------------------------------------------------------


def test_route_and_endpoint_come_from_the_ticket():
    shape = parse_ticket("SIG-109", "Alta de persona", HOUSE)
    assert shape.route == "/erp/talent/people"
    assert shape.endpoint_method == "POST"
    assert shape.endpoint_path == "/api/v1/talent/people"
    assert shape.testable


def test_criteria_are_split_into_given_when_then():
    criteria = parse_ticket("SIG-109", "t", HOUSE).criteria
    assert len(criteria) == 2
    assert criteria[0].name == "Alta correcta"
    assert criteria[0].given.lower().startswith("dado")
    assert criteria[0].then.lower().startswith("entonces")


def test_no_aplica_counts_as_absent():
    """The formatter writes «(no aplica)»; treating it as a value would send
    every plan to a route that does not exist."""
    ticket = HOUSE.replace("/erp/talent/people", "(no aplica)")
    shape = parse_ticket("SIG-1", "t", ticket)
    assert shape.route == ""
    assert not shape.testable
    assert "ruta" in shape.why_not_testable()


def test_a_bare_path_defaults_to_post():
    shape = parse_ticket("SIG-1", "t", HOUSE.replace("POST /api/v1/talent/people", "/api/v1/people"))
    assert (shape.endpoint_method, shape.endpoint_path) == ("POST", "/api/v1/people")


def test_a_ticket_without_an_endpoint_is_not_testable():
    ticket = HOUSE.replace("🔌 Endpoint Backend: POST /api/v1/talent/people", "🔌 Endpoint Backend: (no aplica)")
    shape = parse_ticket("SIG-1", "t", ticket)
    assert not shape.testable
    assert "endpoint" in shape.why_not_testable()


def test_template_placeholder_criteria_are_skipped():
    ticket = HOUSE + "\n📌 Criterio 3: Criterio X\n\n* Describe el criterio aquí\n"
    assert len(parse_ticket("SIG-1", "t", ticket).criteria) == 2


def test_a_ticket_with_no_criteria_section_yields_none():
    assert parse_ticket("SIG-1", "t", "sin formato de casa").criteria == []


# --- turning a criterion into steps -------------------------------------------


GOOD_ANSWER = json.dumps({
    "steps": [
        {"action": "goto", "target": "/erp/talent/people"},
        {"action": "type_label", "target": "Nombre", "value": "Prueba QA"},
        {"action": "click_text", "target": "Guardar"},
    ],
    "ui_text": "Persona registrada",
})


def test_steps_are_read_from_the_answer():
    steps, ui_text, _ = parse_steps(GOOD_ANSWER, "/erp/talent/people")
    assert [s.action for s in steps] == ["goto", "type_label", "click_text"]
    assert ui_text == "Persona registrada"


def test_an_answer_wrapped_in_fences_is_still_read():
    steps, _, _ = parse_steps(f"```json\n{GOOD_ANSWER}\n```", "/erp/talent/people")
    assert steps


def test_an_unknown_action_is_refused_not_dropped():
    """Silently dropping it would leave a plan that validates and passes while
    never doing the thing that mattered."""
    answer = json.dumps({"steps": [{"action": "ejecutar_sql", "target": "drop"}], "ui_text": "ok"})
    with pytest.raises(AuthoringError, match="no permitida"):
        parse_steps(answer, "/x")


def test_a_plan_that_starts_elsewhere_is_corrected():
    answer = json.dumps({"steps": [{"action": "click_text", "target": "Guardar"}], "ui_text": "ok"})
    steps, _, _ = parse_steps(answer, "/erp/talent/people")
    assert steps[0].action == "goto"
    assert steps[0].target == "/erp/talent/people"


def test_an_answer_without_ui_text_is_refused():
    answer = json.dumps({"steps": [{"action": "goto", "target": "/x"}]})
    with pytest.raises(AuthoringError, match="qué debería verse"):
        parse_steps(answer, "/x")


def test_an_answer_that_is_not_json_is_refused():
    with pytest.raises(AuthoringError, match="JSON"):
        parse_steps("lo siento, no puedo", "/x")


# --- the whole plan -----------------------------------------------------------


def test_the_endpoint_is_taken_from_the_ticket_not_the_model(monkeypatch):
    """The endpoint is what separates a working feature from a screen that
    lies, so the model is never allowed to choose it."""
    sneaky = json.dumps({
        "steps": [{"action": "goto", "target": "/erp/talent/people"}],
        "ui_text": "Persona registrada",
        "api_path": "/algo/que/siempre/responde",
    })
    monkeypatch.setattr(authoring_module, "_run_claude", lambda *a, **k: sneaky)

    plan = author_plan("SIG-109", "Alta", HOUSE)
    for case in plan.cases:
        assert case.expectation.api_path == "/api/v1/talent/people"
        assert case.expectation.api_method == "POST"


def test_a_case_is_written_per_criterion(monkeypatch):
    monkeypatch.setattr(authoring_module, "_run_claude", lambda *a, **k: GOOD_ANSWER)
    plan = author_plan("SIG-109", "Alta", HOUSE)
    assert len(plan.cases) == 2
    assert plan.route == "/erp/talent/people"
    assert "SIG-109" in plan.derived_from
    plan.validate()


def test_an_untestable_ticket_is_refused_with_a_reason(monkeypatch):
    monkeypatch.setattr(authoring_module, "_run_claude", lambda *a, **k: GOOD_ANSWER)
    ticket = HOUSE.replace("🔌 Endpoint Backend: POST /api/v1/talent/people", "🔌 Endpoint Backend: (no aplica)")
    with pytest.raises(AuthoringError, match="endpoint"):
        author_plan("SIG-1", "t", ticket)


def test_one_unwritable_criterion_does_not_cost_the_others(monkeypatch):
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        # Both attempts for the first criterion fail, then it recovers.
        return "no puedo" if calls["n"] <= 2 else GOOD_ANSWER

    monkeypatch.setattr(authoring_module, "_run_claude", flaky)
    plan = author_plan("SIG-109", "Alta", HOUSE)
    assert len(plan.cases) == 1
    assert plan.notes and "No se pudo escribir" in plan.notes[0]


def test_a_plan_nobody_could_write_raises(monkeypatch):
    monkeypatch.setattr(authoring_module, "_run_claude", lambda *a, **k: "no puedo")
    with pytest.raises(AuthoringError, match="ningún caso"):
        author_plan("SIG-109", "Alta", HOUSE)


def test_the_plan_round_trips_to_disk(monkeypatch, tmp_path):
    from meta_harness.qa.plan import TestPlan

    monkeypatch.setattr(authoring_module, "_run_claude", lambda *a, **k: GOOD_ANSWER)
    plan = author_plan("SIG-109", "Alta", HOUSE)
    path = plan.save(tmp_path / "plan.json")
    assert TestPlan.load(path).to_dict() == plan.to_dict()


def test_a_read_only_criterion_is_declared_not_inferred():
    """Un criterio visual apaga media comprobación, así que ha de pedirse."""
    answer = json.dumps(
        {
            "steps": [{"action": "goto", "target": "/erp/talent/people"}],
            "ui_text": "Nombres",
            "solo_lectura": True,
        }
    )
    _, _, reads_only = parse_steps(answer, "/erp/talent/people")
    assert reads_only is True

    # Sin declararlo, se sigue exigiendo el endpoint: el silencio no es permiso.
    _, _, reads_only = parse_steps(GOOD_ANSWER, "/erp/talent/people")
    assert reads_only is False
