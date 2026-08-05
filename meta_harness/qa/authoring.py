"""Turn a ticket's acceptance criteria into a plan a human can review.

This is the one place a model is involved, and its job is deliberately narrow:
given a criterion written as Given/When/Then, produce the clicks that exercise
it. That is the part no parser can do — «cuando se confirme el formulario»
becomes a sequence of actions only if something understands the sentence.

What the model is **not** allowed to decide:

    the route      — read from the ticket
    the endpoint   — read from the ticket
    the verbs      — a fixed list of seven

The endpoint is the whole reason a test can tell a working feature from a
screen that lies, so letting a model guess it would remove the only thing
holding the check to reality. It is copied from the ticket in code, after the
model has answered, and a plan whose steps use an unknown verb is rejected
rather than repaired.

Output is a plan, not a run. Nothing executes here.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from meta_harness.claude_output import assert_usable
from meta_harness.qa.plan import (
    ACTIONS,
    ACTION_GOTO,
    Expectation,
    PlanError,
    TestCase,
    TestPlan,
    TestStep,
)
from meta_harness.qa import reconcile as reconciliation
from meta_harness.qa import plan_bench
from meta_harness.qa.flow import FlowMap, describe as describe_flow, explore
from meta_harness.qa.ticket_shape import Criterion, TicketShape, parse_ticket
from meta_harness.qa.vocabulary import Vocabulary, describe
from meta_harness.ticket_generator import CLAUDE_TIMEOUT_ENV_VAR, _find_claude

OnStep = Optional[Callable[[str], None]]

DEFAULT_TIMEOUT_S = 180.0
MAX_ATTEMPTS = 2

PROMPT = (
    "Convierte un criterio de aceptación en pasos de navegador.\n\n"
    "Recibirás JSON con la ruta de la pantalla y un criterio en formato "
    "Dado/Cuando/Entonces.\n\n"
    "Devuelve SOLO JSON:\n"
    '{"steps": [{"action": "...", "target": "...", "value": "..."}], '
    '"ui_text": "<texto corto que debe verse al terminar>", '
    '"solo_lectura": false}\n\n'
    "Acciones permitidas, ninguna otra:\n"
    "- goto        target = la ruta\n"
    "- click       target = selector CSS\n"
    "- click_text  target = el texto visible del botón o enlace\n"
    "- type        target = selector CSS, value = lo que se escribe\n"
    "- type_label  target = la etiqueta visible del campo, value = lo que se escribe\n"
    "- wait_text   target = texto que debe aparecer\n"
    "- wait_selector target = selector CSS que debe aparecer\n\n"
    "Reglas:\n"
    "- Prefiere click_text y type_label: un selector CSS se rompe al primer rediseño, "
    "un texto visible no.\n"
    "- El primer paso siempre es goto con la ruta dada.\n"
    "- ui_text debe ser una frase corta que el usuario vería, sacada del 'entonces'. "
    "No inventes mensajes de éxito que el criterio no mencione.\n"
    "- Usa datos de prueba obvios y desechables (por ejemplo 'Prueba QA').\n"
    "- No inventes endpoints ni URLs: solo la ruta que se te da.\n"
    "- solo_lectura = true SOLO si el criterio se limita a describir lo que se "
    "ve y no guarda nada (por ejemplo «el formulario muestra los campos X, Y, "
    "Z»). Si el criterio crea, edita o borra algo, es false. Ante la duda, "
    "false: marcarlo mal apaga la mitad de la comprobación.\n"
    "- Los target salen de la pantalla, no de tu memoria: si se te da la lista "
    "de palabras que aparecen en ella, cópialas tal cual."
)


@dataclass(frozen=True)
class Strategy:
    """One way of approaching a plan, so candidates differ on purpose.

    Sampling the same prompt three times gives three near-identical plans and
    measures the model's noise. Asking for genuinely different approaches gives
    the benchmark something to choose between: the short path and the careful
    one fail in different places, and which of those matters depends on the app,
    not on a preference decided in advance.
    """

    name: str
    hint: str


STRATEGIES = (
    Strategy(
        "directo",
        "el camino más corto que demuestre el criterio. Solo los pasos "
        "imprescindibles, sin esperas de adorno.",
    ),
    Strategy(
        "explicito",
        "espera a ver cada pantalla antes de actuar sobre ella, y rellena todos "
        "los campos obligatorios aunque el criterio no los nombre.",
    ),
    Strategy(
        "verificador",
        "después de cada acción que cambie de pantalla, comprueba con wait_text "
        "que la nueva se ve, para que un fallo señale el paso exacto.",
    ),
)

# Cuántos planes se escriben por defecto. Cada uno cuesta una llamada al modelo
# por criterio, así que subirlo mejora la elección y alarga la espera.
DEFAULT_CANDIDATES = 2


class AuthoringError(RuntimeError):
    """A plan could not be produced for this ticket."""


def _timeout() -> float:
    raw = os.getenv(CLAUDE_TIMEOUT_ENV_VAR)
    try:
        value = float(raw) if raw else DEFAULT_TIMEOUT_S
    except ValueError:
        return DEFAULT_TIMEOUT_S
    return value if value > 0 else DEFAULT_TIMEOUT_S


def _strip_fences(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n", "", text)
        text = re.sub(r"\n```$", "", text)
    return text.strip()


def _run_claude(prompt: str, payload: str, timeout_s: float) -> str:
    claude_path = _find_claude()
    try:
        completed = subprocess.run(
            [claude_path, "-p", prompt, "--output-format", "text"],
            input=payload, capture_output=True, text=True, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        raise AuthoringError(f"Claude tardó más de {timeout_s}s escribiendo los pasos") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()[:200] or "(sin salida)"
        raise AuthoringError(f"Claude falló ({completed.returncode}): {detail}")
    return assert_usable(completed.stdout, action="la redacción del plan de prueba")


def parse_steps(raw_output: str, route: str) -> tuple:
    """Read the model's answer into steps, expected text, and whether it writes.

    Unknown verbs are refused, not silently dropped: a plan missing the step
    that mattered would still validate and would still pass, which is worse
    than no plan at all.
    """
    text = _strip_fences(raw_output)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise AuthoringError(f"Claude no devolvió JSON: {text[:200]!r}")
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise AuthoringError(f"Claude no devolvió JSON válido: {exc}") from exc

    if not isinstance(payload, dict):
        raise AuthoringError("la respuesta no es un objeto JSON")

    raw_steps = payload.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise AuthoringError("la respuesta no trae pasos")

    steps: List[TestStep] = []
    for index, raw in enumerate(raw_steps):
        if not isinstance(raw, dict):
            raise AuthoringError(f"el paso {index + 1} no es un objeto")
        action = str(raw.get("action", "")).strip()
        if action not in ACTIONS:
            raise AuthoringError(f"el paso {index + 1} usa una acción no permitida: {action!r}")
        steps.append(
            TestStep(
                action=action,
                target=str(raw.get("target", "")).strip(),
                value=str(raw.get("value", "")),
            )
        )

    # The plan must start on the page the ticket declares, whatever the model
    # returned — a run that begins somewhere else is testing another screen.
    if steps[0].action != ACTION_GOTO or steps[0].target != route:
        steps.insert(0, TestStep(ACTION_GOTO, route))

    ui_text = str(payload.get("ui_text", "")).strip()
    if not ui_text:
        raise AuthoringError("la respuesta no dice qué debería verse al terminar")
    return steps, ui_text, bool(payload.get("solo_lectura", False))


def _case_for(
    criterion: Criterion,
    shape: TicketShape,
    index: int,
    timeout_s: float,
    flow: Optional[FlowMap] = None,
    matches: Optional[List[reconciliation.Match]] = None,
    strategy: str = "",
) -> TestCase:
    payload = json.dumps(
        {
            "route": shape.route,
            "criterion": {
                "name": criterion.name,
                "given": criterion.given,
                "when": criterion.when,
                "then": criterion.then,
            },
        },
        ensure_ascii=False,
        indent=2,
    )

    prompt = (
        PROMPT
        + describe_flow(flow)
        + reconciliation.describe(matches or [])
        + (f"\n\nEnfoque para este plan: {strategy}" if strategy else "")
    )

    last_error = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        raw = _run_claude(prompt, payload, timeout_s)
        try:
            steps, ui_text, reads_only = parse_steps(raw, shape.route)
        except AuthoringError as exc:
            last_error = str(exc)
            if attempt == MAX_ATTEMPTS:
                raise AuthoringError(f"{criterion.name}: {last_error}") from exc
            continue

        return TestCase(
            name=f"{index:02d}-{_slug(criterion.name)}",
            criterion=criterion.text or criterion.name,
            steps=steps,
            # Built here, from the ticket. The model never sees these fields,
            # so it cannot move the goalposts to something it can pass.
            expectation=Expectation(
                ui_text=ui_text,
                # El endpoint sigue saliendo del ticket. Lo único que decide el
                # modelo es si este criterio llega a llamarlo.
                api_path="" if reads_only else shape.endpoint_path,
                api_method=shape.endpoint_method or "POST",
                reads_only=reads_only,
            ),
        )
    raise AuthoringError(f"{criterion.name}: {last_error}")


def _archive_root() -> Path:
    """Where scored candidates are filed, alongside every other Hermes run."""
    return Path(os.getenv("MH_QA_PLAN_ARCHIVE", "qa/plan-benchmarks"))


def _map_flow(environment: str, route: str, on_step: OnStep) -> Optional[FlowMap]:
    """Walk the flow once and record every view, or give up quietly.

    Shared by every criterion of the ticket: one browser, one page load. The
    cost is a few seconds per plan, against a plan written entirely against
    labels that do not exist — which is what the previous version produced.

    Every failure here is non-fatal. An unreachable environment means the plan
    is authored from the ticket alone, exactly as before, and the run will say
    so when the steps miss.
    """
    # Imported late: authoring a plan must not require a browser to be
    # installed, and the webapp imports this module at startup.
    from meta_harness.qa.auth import authenticate, token_for
    from meta_harness.qa.browser import Browser
    from meta_harness.qa.environments import EnvironmentError_, resolve_environment

    try:
        target = resolve_environment(environment)
    except EnvironmentError_ as exc:
        if on_step:
            on_step(f"sin vocabulario de pantalla ({exc})")
        return None

    url = target.ui_url.rstrip("/") + "/" + route.lstrip("/")
    if on_step:
        on_step(f"recorriendo el flujo desde {url}…")
    try:
        with Browser() as browser:
            authenticate(browser, target.ui_url, token_for(environment))
            flow = explore(browser, url)
    except Exception as exc:  # noqa: BLE001 - authoring survives a dead target
        if on_step:
            on_step(f"no se pudo recorrer el flujo: {exc}")
        return None

    if on_step:
        for view in sorted(flow.views, key=lambda v: v.depth):
            on_step(f"  vista «{view.name}» — {view.how_to_reach()}")
        on_step(f"flujo recorrido: {len(flow.views)} vista(s); {flow.stopped_because}")
    return None if flow.empty else flow


def _slug(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in " -_" else "" for ch in value).strip()
    return re.sub(r"\s+", "-", cleaned).lower()[:40] or "criterio"


def author_plan(
    ticket_id: str,
    title: str,
    description: str,
    *,
    environment: str = "local",
    timeout_s: Optional[float] = None,
    on_step: OnStep = None,
    candidate_count: int = DEFAULT_CANDIDATES,
) -> TestPlan:
    """Write a reviewable plan for one ticket.

    Raises rather than guessing when the ticket does not declare a route and an
    endpoint: a plan without them could only check that a page renders, which
    is what the previous QA flow already did.
    """
    shape = parse_ticket(ticket_id, title, description)
    if not shape.testable:
        raise AuthoringError(shape.why_not_testable())

    resolved_timeout = timeout_s if timeout_s is not None else _timeout()
    if on_step:
        on_step(f"{ticket_id}: {len(shape.criteria)} criterio(s) sobre {shape.route}")

    flow = _map_flow(environment, shape.route, on_step)

    # Qué afirma el ticket frente a qué hay. Se hace una vez por ticket, entre
    # recorrer el flujo y escribir el primer paso, porque el resultado cambia lo
    # que se le puede pedir al modelo — y porque lo que no existe hay que
    # decirlo, no rodearlo.
    matches: List[reconciliation.Match] = []
    if flow is not None:
        vocabulary = flow.merged()
        terms = reconciliation.mentioned_elements(shape, resolved_timeout)
        matches = reconciliation.reconcile(terms, vocabulary)
        matches = reconciliation.relate_remaining(matches, vocabulary, resolved_timeout)
        if on_step and matches:
            absent = sum(1 for match in matches if match.missing)
            on_step(
                f"{len(matches)} elemento(s) del ticket contrastados; "
                f"{absent} sin equivalente en el flujo"
            )

    common_notes = (
        [f"Criterio omitido (plantilla sin rellenar): {name}" for name in shape.skipped]
        + reconciliation.summarize(matches)
    )

    candidates: List[TestPlan] = []
    for number, strategy in enumerate(STRATEGIES[:max(1, candidate_count)], start=1):
        if on_step:
            on_step(f"escribiendo el plan {number}/{min(len(STRATEGIES), candidate_count)}…")
        try:
            candidates.append(
                _write_one_plan(
                    shape, title, environment, resolved_timeout,
                    flow, matches, strategy, common_notes, on_step,
                )
            )
        except AuthoringError as exc:
            if on_step:
                on_step(f"  plan {number} descartado: {exc}")

    if not candidates:
        raise AuthoringError(f"{ticket_id}: no se pudo escribir ningún plan")

    return _pick(candidates, flow, on_step)


def _write_one_plan(
    shape: TicketShape,
    title: str,
    environment: str,
    timeout_s: float,
    flow: Optional[FlowMap],
    matches: List[reconciliation.Match],
    strategy: "Strategy",
    common_notes: List[str],
    on_step: OnStep,
) -> TestPlan:
    """One whole candidate: every criterion written under one approach."""
    cases: List[TestCase] = []
    errors: List[str] = []
    for index, criterion in enumerate(shape.criteria, start=1):
        if on_step:
            on_step(f"  [{index}/{len(shape.criteria)}] {criterion.name}…")
        try:
            cases.append(
                _case_for(criterion, shape, index, timeout_s, flow, matches, strategy.hint)
            )
        except AuthoringError as exc:
            # One unwritable criterion should not cost the others.
            errors.append(str(exc))

    if not cases:
        raise AuthoringError("; ".join(errors)[:300] or "sin casos")

    plan = TestPlan(
        ticket_id=shape.ticket_id,
        ticket_title=title,
        environment=environment,
        route=shape.route,
        cases=cases,
        derived_from=f"criterios de aceptación de {shape.ticket_id} ({strategy.name})",
        notes=common_notes + [f"No se pudo escribir: {error}" for error in errors],
        correspondence=[match.to_dict() for match in matches],
    )
    plan.validate()
    return plan


def _pick(
    candidates: List[TestPlan], flow: Optional[FlowMap], on_step: OnStep
) -> TestPlan:
    """Score the candidates against the real flow and return the best.

    Without a flow map there is nothing to score against, so the first plan is
    returned unmeasured — and says so, rather than implying it won a contest it
    never entered.
    """
    if flow is None or flow.empty:
        chosen = candidates[0]
        chosen.notes.append(
            "Sin mapa del flujo: el plan no se pudo medir contra la app y se "
            "eligió el primero escrito."
        )
        return chosen

    archive = _archive_root()
    scores = []
    for candidate in candidates:
        name = candidate.derived_from.split("(")[-1].rstrip(")") or "candidato"
        result = plan_bench.score_plan(candidate, flow, candidate_name=name)
        try:
            plan_bench.write_run_dir(result, archive, flow)
            plan_bench.record_in_frontier(result, archive / "frontier.json")
        except Exception as exc:  # noqa: BLE001 - no poder archivar no invalida la medida
            if on_step:
                on_step(f"  no se pudo archivar {name}: {exc}")
        scores.append(result)
        if on_step:
            on_step(f"  {result.summary_line()}")

    best = plan_bench.choose_best(scores)
    chosen = best.plan
    chosen.notes.append(
        f"Elegido entre {len(scores)} plan(es) medidos contra la app: "
        f"{best.summary_line()}."
    )
    for other in scores:
        if other is not best:
            chosen.notes.append(f"Descartado — {other.summary_line()}.")
    if on_step:
        on_step(f"{chosen.ticket_id}: plan con {len(chosen.cases)} caso(s) listo para revisar")
    return chosen
