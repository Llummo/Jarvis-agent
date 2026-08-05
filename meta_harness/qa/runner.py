"""Execute a reviewed plan and collect the evidence.

Deterministic on purpose: the plan decides what happens, this only carries it
out. No model runs here. Two runs of the same plan against the same build do
the same thing, which is what makes a red result worth acting on.

A case passes only when the screen shows what it should **and** the endpoint
the ticket declares actually answered. Checking one without the other is how a
suite ends up green while the feature is broken:

    UI only    — a success toast over a request that never left
    API only   — a 201 the user never sees

Nothing aborts the run. A case that fails is a result, not an exception; the
remaining cases still tell you whether the rest of the feature works.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from meta_harness.qa.auth import AuthError, assert_authenticated, authenticate
from meta_harness.qa.browser import Browser, BrowserError, NetworkCall
from meta_harness.qa.environments import Environment
from meta_harness.qa.plan import (
    ACTION_CLICK,
    ACTION_CLICK_TEXT,
    ACTION_GOTO,
    ACTION_TYPE,
    ACTION_TYPE_LABEL,
    ACTION_WAIT_SELECTOR,
    ACTION_WAIT_TEXT,
    Expectation,
    TestCase,
    TestPlan,
    TestStep,
)

OnStep = Optional[Callable[[str], None]]

RESULT_PASSED = "passed"
RESULT_FAILED = "failed"
RESULT_ERROR = "error"


def _report(on_step: OnStep, message: str) -> None:
    if on_step is not None:
        on_step(message)


@dataclass
class StepOutcome:
    """What happened on one step."""

    action: str
    description: str
    ok: bool
    error: str = ""
    screenshot: str = ""
    # Qué se encontró cuando no coincide literalmente con lo pedido. Una
    # coincidencia aproximada que no se declara es indistinguible de una exacta,
    # y entonces nadie puede juzgar si fue razonable.
    matched_text: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "description": self.description,
            "ok": self.ok,
            "error": self.error,
            "screenshot": self.screenshot,
            "matched_text": self.matched_text,
        }


@dataclass
class CaseResult:
    """What happened on one acceptance criterion."""

    name: str
    criterion: str
    result: str
    steps: List[StepOutcome] = field(default_factory=list)
    ui_ok: bool = False
    ui_detail: str = ""
    api_ok: bool = False
    api_detail: str = ""
    # Si se llegó a mirar la red. Un criterio visual no la mira, y un ✅ junto a
    # «no llama a ningún endpoint» se lee como que la API respondió bien.
    api_checked: bool = True
    api_calls: List[Dict[str, Any]] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def passed(self) -> bool:
        return self.result == RESULT_PASSED

    def why(self) -> str:
        """One line saying what went wrong, in the order a person would look."""
        if self.passed:
            return "UI y API correctos"
        broken = [outcome for outcome in self.steps if not outcome.ok]
        if broken:
            return f"falló el paso «{broken[0].description}»: {broken[0].error}"
        reasons = []
        if not self.ui_ok:
            reasons.append(self.ui_detail or "la UI no mostró lo esperado")
        if not self.api_ok:
            reasons.append(self.api_detail or "la API no respondió como se esperaba")
        return "; ".join(reasons)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "criterion": self.criterion,
            "result": self.result,
            "passed": self.passed,
            "why": self.why(),
            "ui_ok": self.ui_ok,
            "ui_detail": self.ui_detail,
            "api_ok": self.api_ok,
            "api_detail": self.api_detail,
            "api_checked": self.api_checked,
            "api_calls": list(self.api_calls),
            "steps": [outcome.to_dict() for outcome in self.steps],
            "seconds": round(self.seconds, 2),
        }


@dataclass
class RunResult:
    """The whole run."""

    ticket_id: str
    ticket_title: str
    environment: Dict[str, str]
    route: str
    started_at: str
    evidence_dir: Path
    cases: List[CaseResult] = field(default_factory=list)

    @property
    def passed(self) -> List[CaseResult]:
        return [case for case in self.cases if case.passed]

    @property
    def failed(self) -> List[CaseResult]:
        return [case for case in self.cases if not case.passed]

    @property
    def pass_rate(self) -> float:
        return len(self.passed) / len(self.cases) if self.cases else 0.0

    def summary_line(self) -> str:
        return f"{len(self.passed)}/{len(self.cases)} criterios pasaron ({self.pass_rate:.0%})"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticket_id": self.ticket_id,
            "ticket_title": self.ticket_title,
            "environment": self.environment,
            "route": self.route,
            "started_at": self.started_at,
            "evidence_dir": str(self.evidence_dir),
            "summary": self.summary_line(),
            "passed_count": len(self.passed),
            "failed_count": len(self.failed),
            "pass_rate": self.pass_rate,
            "cases": [case.to_dict() for case in self.cases],
        }


def _matches(call: NetworkCall, expectation: Expectation) -> bool:
    if expectation.api_method and call.method.upper() != expectation.api_method.upper():
        return False
    # Compared as a path so one plan runs against local and the deployed site.
    return call.path.rstrip("/") == expectation.api_path.rstrip("/")


def _check_api(calls: List[NetworkCall], expectation: Expectation) -> tuple:
    if expectation.reads_only:
        # No hay nada que comprobar en la red, y decirlo importa: el informe no
        # debe leerse como si el endpoint hubiera respondido bien.
        return True, "criterio visual: no llama a ningún endpoint"
    matching = [call for call in calls if _matches(call, expectation)]
    if not matching:
        seen = ", ".join(sorted({f"{c.method} {c.path}" for c in calls})[:6]) or "ninguna llamada"
        return False, (
            f"la UI nunca llamó a {expectation.api_method} {expectation.api_path} "
            f"(se vieron: {seen})"
        )
    if expectation.api_status_range == "any":
        return True, f"{expectation.api_method} {expectation.api_path} fue llamado"

    good = [call for call in matching if call.ok]
    if not good:
        statuses = ", ".join(str(call.status) for call in matching)
        return False, f"{expectation.api_method} {expectation.api_path} respondió {statuses}"
    return True, f"{expectation.api_method} {expectation.api_path} respondió {good[0].status}"


def _check_ui(browser: Browser, expectation: Expectation) -> tuple:
    if expectation.ui_selector:
        if not browser.visible(expectation.ui_selector):
            return False, f"no se ve el elemento {expectation.ui_selector!r}"
    if expectation.ui_text:
        if not browser.page_contains(expectation.ui_text):
            return False, f"la página no muestra {expectation.ui_text!r}"
    shown = expectation.ui_text or expectation.ui_selector
    return True, f"se ve {shown!r}"


def _perform(browser: Browser, step: TestStep, environment: Environment) -> str:
    """Carry out one step; returns the text actually matched, when it differs.

    Raises BrowserError if the step cannot be done.
    """
    if step.action == ACTION_GOTO:
        browser.goto(environment.url_for(step.target))
    elif step.action == ACTION_CLICK:
        browser.click(step.target)
    elif step.action == ACTION_CLICK_TEXT:
        found = browser.find_best_match(step.target)
        if not found:
            raise BrowserError(f"no se encontró nada parecido a {step.target!r}")
        browser.click(found["selector"])
        return "" if found["exact"] else found["text"]
    elif step.action == ACTION_TYPE:
        browser.type_into(step.target, step.value)
    elif step.action == ACTION_TYPE_LABEL:
        # Un desplegable y un área de texto también son campos que se rellenan;
        # limitarlo a <input> dejaba fuera «Fuente», que es un <select>.
        found = browser.find_best_match(step.target, role="input, select, textarea")
        if not found:
            raise BrowserError(f"no se encontró un campo parecido a {step.target!r}")
        browser.type_into(found["selector"], step.value)
        return "" if found["exact"] else found["text"]
    elif step.action == ACTION_WAIT_TEXT:
        browser.wait_for(
            f"document.body.innerText.toLowerCase().includes({step.target.lower()!r})",
            message=f"nunca apareció el texto {step.target!r}",
        )
    elif step.action == ACTION_WAIT_SELECTOR:
        browser.wait_for(
            f"document.querySelector({step.target!r}) !== null",
            message=f"nunca apareció {step.target!r}",
        )
    else:  # pragma: no cover - plan validation rejects unknown actions first
        raise BrowserError(f"acción no soportada: {step.action}")
    return ""


def _run_case(
    browser: Browser,
    case: TestCase,
    environment: Environment,
    evidence_dir: Path,
    on_step: OnStep,
) -> CaseResult:
    started = time.monotonic()
    result = CaseResult(
        name=case.name,
        criterion=case.criterion,
        result=RESULT_PASSED,
        api_checked=not (case.expectation and case.expectation.reads_only),
    )

    # Each case is judged on the traffic it causes, not on what the page did
    # while it was loading.
    browser.reset_network()

    for index, step in enumerate(case.steps, start=1):
        description = step.describe()
        _report(on_step, f"  {case.name} · paso {index}: {description}")
        shot = evidence_dir / f"{_slug(case.name)}-{index:02d}.png"
        try:
            matched = _perform(browser, step, environment)
            # Una navegación puede acabar en el login por redirección del
            # cliente: la URL sigue siendo la pedida, la pantalla no.
            assert_authenticated(browser, where=f"el paso «{description}»")
            try:
                browser.screenshot(shot)
                shot_path = str(shot)
            except BrowserError:
                shot_path = ""
            result.steps.append(
                StepOutcome(step.action, description, True, screenshot=shot_path, matched_text=matched)
            )
        except (BrowserError, AuthError) as exc:
            try:
                browser.screenshot(shot)
                shot_path = str(shot)
            except BrowserError:
                shot_path = ""
            result.steps.append(
                StepOutcome(step.action, description, False, error=str(exc)[:300], screenshot=shot_path)
            )
            # The remaining steps assume this one worked, so stop this case —
            # but the run continues with the next criterion.
            result.result = RESULT_FAILED
            result.seconds = time.monotonic() - started
            result.api_calls = [call.to_dict() for call in browser.api_calls()]
            return result

    calls = browser.api_calls()
    result.api_calls = [call.to_dict() for call in calls]
    result.ui_ok, result.ui_detail = _check_ui(browser, case.expectation)
    result.api_ok, result.api_detail = _check_api(calls, case.expectation)
    # Both halves, always.
    result.result = RESULT_PASSED if (result.ui_ok and result.api_ok) else RESULT_FAILED
    result.seconds = time.monotonic() - started
    return result


def _slug(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in value)[:60].strip("-") or "case"


def run_plan(
    plan: TestPlan,
    environment: Environment,
    *,
    evidence_root: Path,
    on_step: OnStep = None,
) -> RunResult:
    """Execute every case in the plan and gather the evidence."""
    plan.validate()

    started_at = datetime.now(timezone.utc)
    evidence_dir = Path(evidence_root) / f"{plan.ticket_id}-{started_at.strftime('%Y%m%dT%H%M%SZ')}"
    evidence_dir.mkdir(parents=True, exist_ok=True)

    run = RunResult(
        ticket_id=plan.ticket_id,
        ticket_title=plan.ticket_title,
        environment=environment.describe(),
        route=plan.route,
        started_at=started_at.isoformat(),
        evidence_dir=evidence_dir,
    )

    _report(on_step, f"Ejecutando {len(plan.cases)} criterio(s) contra {environment.ui_url}…")
    with Browser() as browser:
        if environment.auth_token:
            _report(on_step, "Instalando la sesión en el navegador…")
            authenticate(browser, environment.ui_url, environment.auth_token)
        else:
            # Sin token, todo aterrizará en el login. Se avisa una vez aquí en
            # lugar de repetir el mismo error en cada paso.
            _report(on_step, "Aviso: no hay token configurado para este entorno.")
        for case in plan.cases:
            _report(on_step, f"{case.name}…")
            run.cases.append(_run_case(browser, case, environment, evidence_dir, on_step))
            _report(on_step, f"  → {run.cases[-1].result}: {run.cases[-1].why()}")

    _report(on_step, f"Listo: {run.summary_line()}")
    return run
