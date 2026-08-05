"""Score competing plans against the real UI before any of them is run.

Authoring is the one non-deterministic step here, and it shows: the same ticket
and the same screen produce a different plan every time, some of them good and
some of them referring to a button in the wrong view. Picking one blind and
running it means the pass rate measures the draw as much as the product.

So several plans are written and measured first. What is measured is not
whether Sigo works — nothing is executed — but whether the plan is **carryable
out against the app that exists**:

    anclaje       every target resolves to something really on screen
    alcance       a step that uses a deep view is preceded by the clicks that
                  open it, in order
    cierre        a criterion that writes actually reaches a submit control

Those are exactly the three ways every plan so far has failed, and all three can
be checked from the flow map without touching the app.

Runs are written in the layout the outer loop already reads, so `show-frontier`
ranks candidates and `compare-runs` diffs any two — same as the rework
benchmark, and for the same reason: a number nobody can reproduce is an opinion.

Two failures are counted apart, because they are not equally bad:

    ungrounded    the plan names something that does not exist — it will fail,
                  and the report will blame the product for the plan's mistake
    unreachable   the target exists but the plan never opened its view — the
                  step fails somewhere far from the actual error

Pass rate alone would rank a plan that invents three buttons above one that
merely mis-orders a click, and that is backwards.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from meta_harness.comparability import build_task_selection_metadata
from meta_harness.qa.flow import FlowMap, View
from meta_harness.qa.plan import (
    ACTION_CLICK_TEXT,
    ACTION_TYPE_LABEL,
    ACTION_WAIT_TEXT,
    TestCase,
    TestPlan,
)
from meta_harness.qa.textmatch import score

SCHEMA_VERSION = 1
BENCHMARK_NAME = "qa-plan-groundedness"

# Only these name something a person sees. `goto` takes a route and the CSS
# variants take a selector; neither is a label, and scoring them as one would
# punish a plan for using an escape hatch that is already discouraged.
LABEL_ACTIONS = (ACTION_CLICK_TEXT, ACTION_TYPE_LABEL, ACTION_WAIT_TEXT)

# How close a target has to be to a real label to count as anchored. The same
# bar reconciliation uses: below this it is a resemblance, not the thing.
GROUNDED_ENOUGH = 0.9


@dataclass
class StepVerdict:
    """One step, and whether the app could carry it out."""

    action: str
    target: str
    grounded: bool
    reachable: bool
    label: str = ""
    view: str = ""
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.grounded and self.reachable

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "target": self.target,
            "grounded": self.grounded,
            "reachable": self.reachable,
            "label": self.label,
            "view": self.view,
            "detail": self.detail,
        }


@dataclass
class CaseVerdict:
    """One criterion's steps, judged together."""

    name: str
    steps: List[StepVerdict] = field(default_factory=list)
    closes: bool = True
    close_detail: str = ""

    @property
    def ungrounded(self) -> List[StepVerdict]:
        return [step for step in self.steps if not step.grounded]

    @property
    def unreachable(self) -> List[StepVerdict]:
        return [step for step in self.steps if step.grounded and not step.reachable]

    @property
    def passed(self) -> bool:
        return not self.ungrounded and not self.unreachable and self.closes

    def why(self) -> str:
        if self.passed:
            return "todos los pasos se pueden ejecutar sobre la app real"
        parts = []
        for step in self.ungrounded:
            parts.append(f"«{step.target}» no existe en el flujo")
        for step in self.unreachable:
            parts.append(f"«{step.target}» está en «{step.view}», que el plan no abre")
        if not self.closes:
            parts.append(self.close_detail)
        return "; ".join(parts)

    def to_record(self) -> Dict[str, Any]:
        return {
            "task_name": self.name,
            "passed": self.passed,
            "why": self.why(),
            "ungrounded": len(self.ungrounded),
            "unreachable": len(self.unreachable),
            "closes": self.closes,
            "steps": [step.to_dict() for step in self.steps],
        }


@dataclass
class PlanScore:
    """How carryable-out a whole plan is."""

    candidate_name: str
    plan: TestPlan
    cases: List[CaseVerdict] = field(default_factory=list)
    run_dir: Optional[Path] = None

    @property
    def pass_rate(self) -> float:
        return (
            sum(1 for case in self.cases if case.passed) / len(self.cases)
            if self.cases
            else 0.0
        )

    @property
    def ungrounded(self) -> int:
        return sum(len(case.ungrounded) for case in self.cases)

    @property
    def unreachable(self) -> int:
        return sum(len(case.unreachable) for case in self.cases)

    def summary_line(self) -> str:
        return (
            f"{self.candidate_name}: {self.pass_rate:.0%} de criterios ejecutables, "
            f"{self.ungrounded} paso(s) sin anclaje, {self.unreachable} sin alcance"
        )


def _locate(target: str, flow: FlowMap) -> tuple:
    """The view and label that best answer `target`, or (None, '', 0.0)."""
    best_view: Optional[View] = None
    best_label = ""
    best_score = 0.0
    for view in sorted(flow.views, key=lambda v: v.depth):
        vocabulary = view.vocabulary
        for label in (
            list(vocabulary.actions)
            + list(vocabulary.form_actions)
            + list(vocabulary.fields)
            + list(vocabulary.texts)
        ):
            value = score(target, label)
            # Empates a favor de la vista menos profunda: el orden del recorrido
            # ya las trae ordenadas, así que solo gana una estrictamente mejor.
            if value > best_score:
                best_view, best_label, best_score = view, label, value
    return best_view, best_label, best_score


def _reached(needed: List[str], opened: List[str]) -> bool:
    """Whether `opened` already contains `needed`, in order."""
    cursor = 0
    for done in opened:
        if cursor < len(needed) and score(needed[cursor], done) >= GROUNDED_ENOUGH:
            cursor += 1
    return cursor >= len(needed)


def _submits_now(target: str, opened: List[str], flow: FlowMap) -> bool:
    """Whether clicking `target` at this point confirms an open form.

    The position matters, not just the wording. In Sigo the button that submits
    the person form is also called «Agregar persona», so matching by name alone
    counted the click that *opened* the form as the one that sent it, and a plan
    that never confirmed anything scored as complete.
    """
    for view in flow.views:
        if not any(target.strip().lower() == label.strip().lower()
                   for label in view.vocabulary.form_actions):
            continue
        # Su vista ya tiene que estar abierta antes de este clic: si este es el
        # clic que la abre, está abriendo, no enviando.
        if view.path and _reached(list(view.path), opened):
            return True
    return False


def judge_case(case: TestCase, flow: FlowMap) -> CaseVerdict:
    """Whether every step of one criterion could actually be carried out."""
    verdict = CaseVerdict(name=case.name)

    # Lo que el plan lleva abierto en cada punto. Se reconstruye paso a paso
    # porque el alcance depende del orden: pulsar «Agregar persona» después de
    # escribir en su formulario no cuenta.
    opened: List[str] = []
    submitted = False

    for step in case.steps:
        if step.action not in LABEL_ACTIONS:
            continue

        view, label, value = _locate(step.target, flow)
        grounded = value >= GROUNDED_ENOUGH and view is not None
        if not grounded:
            verdict.steps.append(
                StepVerdict(
                    action=step.action,
                    target=step.target,
                    grounded=False,
                    reachable=False,
                    detail="ninguna vista del flujo ofrece nada con ese nombre",
                )
            )
            if step.action == ACTION_CLICK_TEXT:
                opened.append(step.target)
            continue

        # Alcanzable si los clics que abren su vista ya se dieron, en orden.
        reachable = _reached(list(view.path), opened)

        verdict.steps.append(
            StepVerdict(
                action=step.action,
                target=step.target,
                grounded=True,
                reachable=reachable,
                label=label,
                view=view.name,
                detail="" if reachable else f"hay que pulsar antes: {view.how_to_reach()}",
            )
        )
        if step.action == ACTION_CLICK_TEXT:
            if _submits_now(step.target, opened, flow):
                submitted = True
            opened.append(step.target)

    expectation = case.expectation
    if expectation is not None and not expectation.reads_only and not submitted:
        # Un criterio que escribe tiene que llegar a pulsar algo que envíe. Sin
        # esto, un plan que rellena el formulario y no lo confirma puntúa igual
        # que uno que sí, y luego falla siempre por «nunca llamó al endpoint».
        verdict.closes = False
        verdict.close_detail = (
            "el criterio escribe pero el plan nunca pulsa nada que envíe el formulario"
        )

    return verdict


def score_plan(plan: TestPlan, flow: FlowMap, *, candidate_name: str) -> PlanScore:
    """Judge every criterion of a plan against the mapped flow."""
    return PlanScore(
        candidate_name=candidate_name,
        plan=plan,
        cases=[judge_case(case, flow) for case in plan.cases],
    )


def write_run_dir(result: PlanScore, archive_root: Path, flow: FlowMap) -> Path:
    """Record one candidate where the outer loop already looks for runs."""
    started = datetime.now(timezone.utc)
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    run_dir = Path(archive_root) / f"{result.plan.ticket_id}-{result.candidate_name}-{stamp}"
    (run_dir / "tasks").mkdir(parents=True, exist_ok=True)

    records = [case.to_record() for case in result.cases]
    for record in records:
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in record["task_name"])
        (run_dir / "tasks" / f"{safe}.json").write_text(
            json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    (run_dir / "summary.json").write_text(
        json.dumps(
            {
                "benchmark_name": BENCHMARK_NAME,
                "candidate_name": result.candidate_name,
                "candidate_path": result.plan.ticket_id,
                "eval_metrics": {
                    "eval/pass_rate": result.pass_rate,
                    "eval/total_tasks": len(result.cases),
                    "eval/passed_tasks": sum(1 for case in result.cases if case.passed),
                    # Aparte del pass rate: cambiar tres pasos inalcanzables por
                    # uno inventado no es una mejora, y una sola cifra lo diría.
                    "eval/ungrounded_steps": result.ungrounded,
                    "eval/unreachable_steps": result.unreachable,
                },
                "task_results": records,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "benchmark_name": BENCHMARK_NAME,
                "candidate_name": result.candidate_name,
                "created_at": started.isoformat(),
                "outer_loop": {
                    "benchmark_runner": {
                        "schema_version": SCHEMA_VERSION,
                        "benchmark": BENCHMARK_NAME,
                        "candidate": result.candidate_name,
                        "total_tasks": len(result.cases),
                        # Los criterios son los mismos para todos los candidatos,
                        # así que el hash coincide y compare-runs los admite.
                        "task_selection": build_task_selection_metadata(
                            task_filter=",".join(case.name for case in result.cases),
                            skip_tasks=None,
                        ),
                    },
                    "provenance": {
                        "ticket": result.plan.ticket_id,
                        "route": result.plan.route,
                        "views_mapped": len(flow.views),
                        "exploration": flow.stopped_because,
                    },
                },
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result.run_dir = run_dir
    return run_dir


def record_in_frontier(result: PlanScore, frontier_path: Path, *, notes: str = "") -> None:
    """Rank a candidate against the others by the outer loop's own rule.

    The frontier orders on pass rate, which is Hermes's rule and stays untouched.
    The two failure counts ride along in the notes, so a candidate sitting at the
    top with inventions recorded against it is visibly not the one to run.
    """
    from meta_harness.archive_reader import load_run_summary
    from meta_harness.frontier import FrontierStore

    if result.run_dir is None:
        return
    detail = f"ungrounded={result.ungrounded} unreachable={result.unreachable}"
    FrontierStore(Path(frontier_path)).upsert_from_summary(
        load_run_summary(result.run_dir), notes=f"{notes} {detail}".strip()
    )


def choose_best(results: Sequence[PlanScore]) -> Optional[PlanScore]:
    """The candidate to actually run.

    Ordered by pass rate, then by fewest inventions, then by fewest ordering
    mistakes. The tie-breaks are what the frontier's single number cannot say,
    and they are ordered by which failure costs more to read in a report.
    """
    if not results:
        return None
    return sorted(
        results,
        key=lambda r: (-r.pass_rate, r.ungrounded, r.unreachable, len(r.plan.cases) * -1),
    )[0]
