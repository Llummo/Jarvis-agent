"""A test plan you can read before anything clicks.

This is the artefact that makes the whole flow auditable. A model explores the
feature once and writes one of these; from then on the plan is executed
mechanically, so two runs of the same plan do the same thing. Between the two
there is a document a human can read, correct, or throw away.

Deliberately small. Every step is one verb with one argument, because a plan
that needs interpretation at run time is a program, and a program the model
wrote is not something to run against a QA database unreviewed.

A criterion passes only when **both** halves hold:

    the UI shows what it should      — the person sees the right thing
    the expected endpoint answered   — the system actually did the work

Either alone is a false positive waiting to happen. A green toast proves
nothing if nothing was saved; a 201 proves nothing if the screen never shows it.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# The verbs a plan may use. Kept short on purpose: anything not expressible
# here is a gap to discuss, not a reason to allow arbitrary code.
ACTION_GOTO = "goto"
ACTION_CLICK = "click"
ACTION_CLICK_TEXT = "click_text"
ACTION_TYPE = "type"
ACTION_TYPE_LABEL = "type_label"
ACTION_WAIT_TEXT = "wait_text"
ACTION_WAIT_SELECTOR = "wait_selector"

ACTIONS = (
    ACTION_GOTO,
    ACTION_CLICK,
    ACTION_CLICK_TEXT,
    ACTION_TYPE,
    ACTION_TYPE_LABEL,
    ACTION_WAIT_TEXT,
    ACTION_WAIT_SELECTOR,
)

# Actions that need a value as well as a target.
ACTIONS_NEEDING_VALUE = (ACTION_TYPE, ACTION_TYPE_LABEL)


class PlanError(ValueError):
    """A plan is malformed, or asks for something the runner cannot do."""


@dataclass
class TestStep:
    """One thing to do."""

    # These are data, not test classes; pytest collects by name otherwise.
    __test__ = False

    action: str
    # A CSS selector, a visible label, a route, depending on the action.
    target: str = ""
    value: str = ""
    note: str = ""

    def validate(self, index: int) -> None:
        where = f"step {index + 1}"
        if self.action not in ACTIONS:
            raise PlanError(f"{where}: unknown action {self.action!r}. Allowed: {', '.join(ACTIONS)}")
        if not self.target.strip():
            raise PlanError(f"{where}: {self.action} needs a target")
        if self.action in ACTIONS_NEEDING_VALUE and not self.value:
            raise PlanError(f"{where}: {self.action} needs a value to type")

    def describe(self) -> str:
        if self.action in ACTIONS_NEEDING_VALUE:
            return f"{self.action} {self.target!r} = {self.value!r}"
        return f"{self.action} {self.target!r}"


@dataclass
class Expectation:
    """What must be true once the steps have run.

    Both halves are required for the criterion to pass. `api_path` is a path,
    not a URL, so the same plan runs against local and the deployed QA site.
    """

    # Text that must be visible on the page afterwards.
    ui_text: str = ""
    # A selector that must be present and visible afterwards.
    ui_selector: str = ""
    # The endpoint the page must have called, e.g. "/api/v1/talent/people".
    api_path: str = ""
    api_method: str = "POST"
    # Statuses that count as the API having worked.
    api_status_range: str = "2xx"
    # Whether the criterion only describes what is on screen. «El formulario
    # muestra los mismos campos que el de Candidate» writes nothing, so
    # demanding the endpoint made it impossible to pass no matter how well the
    # feature worked. Marked explicitly, never inferred, and shown in the
    # report — a criterion that quietly stops checking the API is worse than
    # one that fails.
    reads_only: bool = False

    def validate(self) -> None:
        if not self.ui_text and not self.ui_selector:
            raise PlanError(
                "an expectation needs something to check on screen "
                "(ui_text or ui_selector) — a passing API alone is not evidence the feature works"
            )
        if self.reads_only:
            if self.api_path.strip():
                raise PlanError(
                    "a read-only expectation must not name an endpoint: "
                    "either the criterion writes, or it does not"
                )
        elif not self.api_path.strip():
            raise PlanError(
                "an expectation needs the endpoint the UI must call — "
                "a screen that looks right without saving anything is the failure this catches"
            )
        if self.api_status_range not in ("2xx", "any"):
            raise PlanError("api_status_range must be '2xx' or 'any'")

    def describe(self) -> str:
        shown = self.ui_text or self.ui_selector
        if self.reads_only:
            return f"se ve {shown!r} (criterio visual: no escribe nada)"
        return f"se ve {shown!r} y {self.api_method} {self.api_path} responde {self.api_status_range}"


@dataclass
class TestCase:
    """One acceptance criterion, turned into something executable."""

    # These are data, not test classes; pytest collects by name otherwise.
    __test__ = False

    name: str
    # The criterion this came from, verbatim, so a failure can be traced back
    # to what the ticket actually promised.
    criterion: str = ""
    steps: List[TestStep] = field(default_factory=list)
    expectation: Optional[Expectation] = None

    def validate(self) -> None:
        if not self.name.strip():
            raise PlanError("every test case needs a name")
        if not self.steps:
            raise PlanError(f"{self.name}: a case with no steps checks nothing")
        for index, step in enumerate(self.steps):
            step.validate(index)
        if self.expectation is None:
            raise PlanError(f"{self.name}: a case with no expectation always passes, which is worse than no case")
        self.expectation.validate()


@dataclass
class TestPlan:
    """Everything to run for one ticket, against one environment."""

    # These are data, not test classes; pytest collects by name otherwise.
    __test__ = False

    ticket_id: str
    ticket_title: str = ""
    environment: str = "local"
    route: str = "/"
    cases: List[TestCase] = field(default_factory=list)
    # Where the steps came from. Recorded so a reviewer can tell a plan derived
    # from real acceptance criteria from one the model improvised.
    derived_from: str = ""
    notes: List[str] = field(default_factory=list)

    def validate(self) -> None:
        if not self.ticket_id.strip():
            raise PlanError("a plan needs the ticket it came from")
        if not self.cases:
            raise PlanError("a plan with no cases checks nothing")
        seen = set()
        for case in self.cases:
            case.validate()
            if case.name in seen:
                raise PlanError(f"duplicate case name: {case.name}")
            seen.add(case.name)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticket_id": self.ticket_id,
            "ticket_title": self.ticket_title,
            "environment": self.environment,
            "route": self.route,
            "derived_from": self.derived_from,
            "notes": list(self.notes),
            "cases": [
                {
                    "name": case.name,
                    "criterion": case.criterion,
                    "steps": [asdict(step) for step in case.steps],
                    "expectation": asdict(case.expectation) if case.expectation else None,
                }
                for case in self.cases
            ],
        }

    def save(self, path: Path) -> Path:
        self.validate()
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        return path

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "TestPlan":
        if not isinstance(payload, dict):
            raise PlanError(f"expected a JSON object, got {type(payload).__name__}")
        raw_cases = payload.get("cases")
        if not isinstance(raw_cases, list):
            raise PlanError("'cases' must be a list")

        cases: List[TestCase] = []
        for index, raw in enumerate(raw_cases):
            if not isinstance(raw, dict):
                raise PlanError(f"case {index} is not an object")
            raw_steps = raw.get("steps") or []
            if not isinstance(raw_steps, list):
                raise PlanError(f"case {index}: 'steps' must be a list")
            steps = [
                TestStep(
                    action=str(s.get("action", "")),
                    target=str(s.get("target", "")),
                    value=str(s.get("value", "")),
                    note=str(s.get("note", "")),
                )
                for s in raw_steps
                if isinstance(s, dict)
            ]
            raw_expectation = raw.get("expectation")
            expectation = None
            if isinstance(raw_expectation, dict):
                expectation = Expectation(
                    ui_text=str(raw_expectation.get("ui_text", "")),
                    ui_selector=str(raw_expectation.get("ui_selector", "")),
                    api_path=str(raw_expectation.get("api_path", "")),
                    api_method=str(raw_expectation.get("api_method", "POST")).upper(),
                    api_status_range=str(raw_expectation.get("api_status_range", "2xx")),
                    reads_only=bool(raw_expectation.get("reads_only", False)),
                )
            cases.append(
                TestCase(
                    name=str(raw.get("name", "")),
                    criterion=str(raw.get("criterion", "")),
                    steps=steps,
                    expectation=expectation,
                )
            )

        plan = cls(
            ticket_id=str(payload.get("ticket_id", "")),
            ticket_title=str(payload.get("ticket_title", "")),
            environment=str(payload.get("environment", "local")),
            route=str(payload.get("route", "/")),
            cases=cases,
            derived_from=str(payload.get("derived_from", "")),
            notes=[str(n) for n in (payload.get("notes") or [])],
        )
        plan.validate()
        return plan

    @classmethod
    def load(cls, path: Path) -> "TestPlan":
        path = Path(path)
        if not path.is_file():
            raise PlanError(f"No test plan at {path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise PlanError(f"{path} is not valid JSON: {exc}") from exc
        return cls.from_dict(payload)
