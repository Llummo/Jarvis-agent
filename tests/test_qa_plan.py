"""El plan de prueba: el artefacto que se revisa antes de que algo haga clic."""

import json

import pytest

from meta_harness.qa.environments import (
    ENV_LOCAL,
    ENV_QA,
    EnvironmentError_,
    resolve_environment,
)
from meta_harness.qa.plan import Expectation, PlanError, TestCase, TestPlan, TestStep


def _expectation(**over):
    payload = {"ui_text": "Persona registrada", "api_path": "/api/v1/talent/people"}
    payload.update(over)
    return Expectation(**payload)


def _case(**over):
    payload = {
        "name": "alta-persona",
        "criterion": "Dado un formulario válido, cuando se confirme, entonces la persona queda registrada",
        "steps": [TestStep("goto", "/erp/talent/people"), TestStep("click_text", "Guardar")],
        "expectation": _expectation(),
    }
    payload.update(over)
    return TestCase(**payload)


def _plan(**over):
    payload = {"ticket_id": "SIG-109", "route": "/erp/talent/people", "cases": [_case()]}
    payload.update(over)
    return TestPlan(**payload)


# --- entornos -----------------------------------------------------------------


def test_local_resolves_with_a_default():
    assert resolve_environment(ENV_LOCAL).ui_url.startswith("http://localhost")


def test_qa_needs_to_be_configured(monkeypatch):
    monkeypatch.delenv("SIGO_QA_URL", raising=False)
    with pytest.raises(EnvironmentError_, match="No URL configured"):
        resolve_environment(ENV_QA)


def test_qa_reads_its_url_from_the_environment(monkeypatch):
    monkeypatch.setenv("SIGO_QA_URL", "http://qa.interno:8080")
    assert resolve_environment(ENV_QA).ui_url == "http://qa.interno:8080"


def test_production_is_not_a_valid_target():
    with pytest.raises(EnvironmentError_, match="Unknown environment"):
        resolve_environment("prod")


@pytest.mark.parametrize(
    "url",
    ["https://sigo.metrica-global.com", "http://prod-sigo.interno", "https://www.sigo.pe"],
)
def test_a_production_looking_url_is_refused(url):
    """Estas pruebas escriben datos reales; un config equivocado no debe alcanzar producción."""
    with pytest.raises(EnvironmentError_, match="production"):
        resolve_environment(ENV_LOCAL, ui_url=url)


def test_url_for_joins_route_without_double_slashes():
    env = resolve_environment(ENV_LOCAL, ui_url="http://localhost:3000/")
    assert env.url_for("/erp/talent/people") == "http://localhost:3000/erp/talent/people"


def test_api_base_falls_back_to_the_ui_host():
    env = resolve_environment(ENV_LOCAL, ui_url="http://localhost:3000")
    assert env.api_base() == "http://localhost:3000"


# --- validación del plan ------------------------------------------------------


def test_a_valid_plan_passes():
    _plan().validate()


def test_a_case_without_expectation_is_rejected():
    """Un caso sin expectativa siempre pasa, que es peor que no tenerlo."""
    with pytest.raises(PlanError, match="always passes"):
        _plan(cases=[_case(expectation=None)]).validate()


def test_an_expectation_needs_something_on_screen():
    with pytest.raises(PlanError, match="ui_text or ui_selector"):
        _expectation(ui_text="", ui_selector="").validate()


def test_an_expectation_needs_an_endpoint():
    """Una pantalla que se ve bien sin guardar nada es justo lo que esto atrapa."""
    with pytest.raises(PlanError, match="endpoint"):
        _expectation(api_path="").validate()


def test_unknown_actions_are_rejected():
    with pytest.raises(PlanError, match="unknown action"):
        _plan(cases=[_case(steps=[TestStep("hackear", "algo")])]).validate()


def test_typing_needs_a_value():
    with pytest.raises(PlanError, match="needs a value"):
        _plan(cases=[_case(steps=[TestStep("type", "#nombre")])]).validate()


def test_steps_need_a_target():
    with pytest.raises(PlanError, match="needs a target"):
        _plan(cases=[_case(steps=[TestStep("click", "")])]).validate()


def test_a_plan_without_cases_is_rejected():
    with pytest.raises(PlanError, match="no cases"):
        _plan(cases=[]).validate()


def test_duplicate_case_names_are_rejected():
    with pytest.raises(PlanError, match="duplicate"):
        _plan(cases=[_case(), _case()]).validate()


def test_plan_round_trips_through_disk(tmp_path):
    original = _plan()
    path = original.save(tmp_path / "plan.json")
    assert TestPlan.load(path).to_dict() == original.to_dict()


def test_a_corrupt_plan_says_so(tmp_path):
    path = tmp_path / "plan.json"
    path.write_text("{roto", encoding="utf-8")
    with pytest.raises(PlanError, match="not valid JSON"):
        TestPlan.load(path)


def test_the_plan_records_where_it_came_from():
    """Para distinguir un plan derivado de criterios reales de uno improvisado."""
    plan = _plan(derived_from="criterios de aceptación de SIG-109")
    assert "criterios" in plan.to_dict()["derived_from"]
