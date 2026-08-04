"""Browser tests derived from what a ticket promises.

The previous QA flow could say «the route answers» and attach a photo. This
executes the acceptance criteria: it clicks, it types, and it checks that the
screen shows what it should **and** that the declared endpoint answered.

Hybrid on purpose. A model reads the ticket once and writes a plan; the plan is
then executed mechanically. Between the two sits a document a human can read
and correct before anything touches the database.

Only `local` and the deployed QA site. Production is not a valid target: these
tests write real data.

Reports are written in Spanish because that is the language of the tickets and
of the people who read them; the code around them is English like the rest.
"""

from meta_harness.qa.authoring import AuthoringError, author_plan
from meta_harness.qa.browser import Browser, BrowserError, NetworkCall
from meta_harness.qa.environments import (
    ENVIRONMENTS,
    ENV_LOCAL,
    ENV_QA,
    Environment,
    resolve_environment,
)
from meta_harness.qa.plan import (
    ACTIONS,
    Expectation,
    PlanError,
    TestCase,
    TestPlan,
    TestStep,
)
from meta_harness.qa.report import render_markdown, save_report
from meta_harness.qa.ticket_shape import Criterion, TicketShape, parse_ticket
from meta_harness.qa.runner import CaseResult, RunResult, StepOutcome, run_plan

__all__ = [
    "ACTIONS",
    "AuthoringError",
    "Criterion",
    "Browser",
    "BrowserError",
    "CaseResult",
    "ENVIRONMENTS",
    "ENV_LOCAL",
    "ENV_QA",
    "Environment",
    "Expectation",
    "NetworkCall",
    "PlanError",
    "RunResult",
    "StepOutcome",
    "TestCase",
    "TestPlan",
    "TestStep",
    "TicketShape",
    "author_plan",
    "parse_ticket",
    "render_markdown",
    "resolve_environment",
    "run_plan",
    "save_report",
]
