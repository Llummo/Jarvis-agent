"""The write side. The only module in this package that changes Linear.

Every plan runs the same three steps per issue, in this order:

    reformat (read)  →  create the replacement  →  cancel the original

The order is the safety property. Creating before cancelling means a failure
at any point leaves the original exactly where it was — the worst outcome is a
duplicate, which a human can see and delete. Cancelling first would, on a
failed create, hide a ticket with nothing to replace it.

Nothing is rolled back. Deleting a freshly created issue to undo a half-done
item destroys the reformatted content, which is the expensive part; reporting
the half-done state and letting a human finish is cheaper and safer.
"""

from __future__ import annotations

from typing import Callable, Optional

from meta_harness.linear_bridge import (
    LinearIssueError,
    LinearReadError,
    create_linear_issue,
    update_linear_issue_state,
)
from meta_harness.rework.models import ReworkOutcome, ReworkPlan, ReworkReport
from meta_harness.ticket_format import format_linear_description
from meta_harness.ticket_reformat import TicketReformatError, reformat_ticket

OnStep = Optional[Callable[[str], None]]

TRACKER = "linear"

# A failure that belongs to one issue rather than to the run.
ITEM_ERRORS = (
    TicketReformatError,
    LinearIssueError,
    LinearReadError,
    ValueError,
)


def _report(on_step: OnStep, message: str) -> None:
    if on_step is not None:
        on_step(message)


def execute_rework_plan(
    plan: ReworkPlan,
    *,
    reformat_timeout_s: Optional[float] = None,
    on_step: OnStep = None,
) -> ReworkReport:
    """Rework every issue in the plan under the chosen parent.

    One issue failing never stops the rest: the whole point of the batch is
    that a list of thirty tickets does not need thirty manual retries because
    the fourth had an unparseable description.
    """
    plan.validate()

    report = ReworkReport(parent_issue_id=plan.parent_issue_id)
    total = len(plan.items)

    for index, item in enumerate(plan.items, start=1):
        label = item.identifier or item.issue_id
        _report(on_step, f"[{index}/{total}] {label}: reformatting…")
        outcome = ReworkOutcome(
            issue_id=item.issue_id,
            identifier=item.identifier,
            original_title=item.original_title,
        )

        try:
            reformatted = reformat_ticket(
                item.issue_id, tracker=TRACKER, timeout_s=reformat_timeout_s
            )
        except ITEM_ERRORS as exc:
            outcome.error = f"reformat failed: {str(exc)[:280]}"
            report.outcomes.append(outcome)
            _report(on_step, f"[{index}/{total}] {label}: {outcome.error}")
            continue

        title = reformatted.formatted_title or reformatted.original_title
        description = reformatted.formatted_description or format_linear_description(
            reformatted.ticket
        )

        _report(on_step, f"[{index}/{total}] {label}: creating the replacement…")
        try:
            created_id = create_linear_issue(
                plan.team_id,
                title,
                description,
                parent_id=plan.parent_issue_id,
            )
        except ITEM_ERRORS as exc:
            # Nothing was written for this item, so the original is untouched.
            outcome.error = f"create failed: {str(exc)[:280]}"
            report.outcomes.append(outcome)
            _report(on_step, f"[{index}/{total}] {label}: {outcome.error}")
            continue

        outcome.created_issue_id = created_id
        outcome.created_title = title

        if not plan.cancel_originals:
            report.outcomes.append(outcome)
            _report(on_step, f"[{index}/{total}] {label}: replacement created.")
            continue

        _report(on_step, f"[{index}/{total}] {label}: moving the original to cancelled…")
        try:
            update_linear_issue_state(item.issue_id, plan.cancelled_state_id)
            outcome.cancelled = True
        except ITEM_ERRORS as exc:
            # The replacement exists; only the tidy-up failed. Deliberately not
            # recorded as an error — the item succeeded at the part that
            # matters, and `needs_attention` is what the report surfaces.
            _report(
                on_step,
                f"[{index}/{total}] {label}: replacement created, but the original "
                f"could not be cancelled ({str(exc)[:120]}).",
            )

        report.outcomes.append(outcome)
        _report(on_step, f"[{index}/{total}] {label}: done.")

    _report(on_step, f"Finished: {report.summary_line()}")
    return report
