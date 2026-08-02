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
    delete_linear_issue,
    get_linear_issue,
    update_linear_issue_state,
)
from meta_harness.rework.history import (
    ReworkHistoryStore,
    build_run_record,
    new_run_id,
)
from meta_harness.rework.models import (
    ReworkOutcome,
    ReworkPlan,
    ReworkReport,
    UndoOutcome,
    UndoReport,
)
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
    history: Optional[ReworkHistoryStore] = None,
    on_step: OnStep = None,
) -> ReworkReport:
    """Rework every issue in the plan under the chosen parent.

    One issue failing never stops the rest: the whole point of the batch is
    that a list of thirty tickets does not need thirty manual retries because
    the fourth had an unparseable description.
    """
    plan.validate()

    report = ReworkReport(parent_issue_id=plan.parent_issue_id, run_id=new_run_id())
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

        # Read the column the original is in *now*, before moving it. Undo has
        # nothing to restore to otherwise, and taking it from the earlier
        # preview would restore a stale column if someone moved the ticket in
        # the meantime.
        try:
            current = get_linear_issue(item.issue_id)
            state = current.get("state") or {}
            outcome.previous_state_id = str(state.get("id") or "")
            outcome.previous_state_name = str(state.get("name") or "")
        except ITEM_ERRORS:
            # Not fatal: the rework still stands, it just cannot be undone
            # automatically. The report says so rather than pretending.
            pass

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

    if report.created:
        # Recorded only when something was actually created — a run that
        # changed nothing has nothing to undo.
        store = history if history is not None else ReworkHistoryStore()
        try:
            store.remember(
                build_run_record(
                    run_id=report.run_id,
                    team_id=plan.team_id,
                    parent_issue_id=plan.parent_issue_id,
                    outcomes=report.outcomes,
                    cancelled_state_id=plan.cancelled_state_id,
                )
            )
        except OSError as exc:
            # Losing undo is bad; failing a completed rework over it is worse.
            _report(on_step, f"Warning: this run could not be saved for undo ({exc}).")
            report.run_id = ""

    _report(on_step, f"Finished: {report.summary_line()}")
    return report


def undo_rework_run(
    run_id: str,
    *,
    history: Optional[ReworkHistoryStore] = None,
    on_step: OnStep = None,
) -> UndoReport:
    """Reverse a run: originals back to their old column, replacements deleted.

    Order is the mirror of execution. The original is restored *first*, then
    the replacement is deleted — so a failure halfway leaves a visible
    duplicate rather than a ticket that has vanished from the board.

    A run stays undoable until every item succeeds. Dropping it on partial
    success would strand whatever was left half-reversed with no way back.
    """
    store = history if history is not None else ReworkHistoryStore()
    run = store.get(run_id)
    if run is None:
        raise ValueError(f"No undoable run with id {run_id!r}")

    items = run.get("items") or []
    report = UndoReport(run_id=run_id)
    total = len(items)

    for index, item in enumerate(items, start=1):
        label = item.get("identifier") or item.get("issue_id") or "?"
        outcome = UndoOutcome(issue_id=str(item.get("issue_id") or ""), identifier=str(label))

        previous_state_id = str(item.get("previous_state_id") or "")
        if item.get("cancelled") and previous_state_id:
            _report(on_step, f"[{index}/{total}] {label}: restoring to {item.get('previous_state_name') or 'its column'}…")
            try:
                update_linear_issue_state(outcome.issue_id, previous_state_id)
                outcome.restored_to = str(item.get("previous_state_name") or previous_state_id)
            except ITEM_ERRORS as exc:
                outcome.error = f"restore failed: {str(exc)[:200]}"
                report.outcomes.append(outcome)
                continue
        elif item.get("cancelled"):
            # Cancelled, but the original column was never captured.
            outcome.error = "original column was not recorded, so it cannot be restored"
            report.outcomes.append(outcome)
            continue

        created_issue_id = str(item.get("created_issue_id") or "")
        if created_issue_id:
            _report(on_step, f"[{index}/{total}] {label}: deleting the replacement…")
            try:
                delete_linear_issue(created_issue_id)
                outcome.deleted_issue_id = created_issue_id
            except ITEM_ERRORS as exc:
                # The original is already back, so the board is safe — there is
                # simply a duplicate left for a human to remove.
                outcome.error = f"replacement not deleted: {str(exc)[:200]}"

        report.outcomes.append(outcome)

    report.complete = not report.failed
    if report.complete:
        store.forget(run_id)
    _report(on_step, f"Undo finished: {report.summary_line()}")
    return report
