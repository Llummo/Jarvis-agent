"""Rewrite an existing ticket so it follows the house format.

Existing tickets are written by many hands and rarely match the template:
some have no user story, some bury the acceptance criteria in prose, some
have no route or endpoint recorded at all. This reads the real ticket,
asks Claude to restructure what is already there into the standard shape,
and renders it with the tracker's own formatter.

Deliberately preview-first: nothing is written back until the caller
explicitly applies the result, so a bad rewrite is always caught by a
human first. The rewrite is also explicitly conservative — it reorganizes
and fills in what the ticket implies, and must not invent new scope.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import Callable, Optional

from meta_harness.clickup_bridge import get_clickup_task, update_clickup_task
from meta_harness.linear_bridge import get_linear_issue, update_linear_issue
from meta_harness.reformat_history import ReformatHistoryStore
from meta_harness.ticket_format import (
    extract_verbatim_blocks,
    extract_visual_resources,
    format_clickup_description,
    format_linear_description,
)
from meta_harness.ticket_generator import (
    _TICKET_FIELDS_SPEC,
    CLAUDE_TIMEOUT_ENV_VAR,
    DEFAULT_CLAUDE_TIMEOUT_S,
    ProposedTicket,
    TicketParseError,
    _find_claude,
    _run_claude,
    parse_proposed_tickets,
)

OnStep = Optional[Callable[[str], None]]


def _report(on_step: OnStep, message: str) -> None:
    if on_step is not None:
        on_step(message)


TRACKERS = ("clickup", "linear")
MAX_ATTEMPTS = 3

REFORMAT_PROMPT = (
    "You are reformatting an EXISTING work ticket so it follows the team's standard structure. "
    "The ticket's current title and body are given below.\n\n"
    "Rewrite it as a single ticket in the required JSON shape. Rules:\n"
    "- Keep the ticket's actual scope. Reorganize, clarify and fill in the structure the ticket "
    "already implies; do NOT invent new requirements, new endpoints or new criteria that the "
    "original does not support.\n"
    "- Preserve any identifier or prefix the title already carries (for example "
    '"TAB-07 | ", "1.26. ", "[Bug] ") exactly as-is at the start of the title.\n'
    "- Pull the route, endpoint, permissions, limits and state models out of the existing prose "
    "and into their proper fields when the ticket mentions them; leave them empty when it does not.\n"
    "- Turn whatever the ticket already states as expectations into properly structured "
    "acceptance criteria. If it truly states none, produce the few that its description directly "
    "implies — no more.\n"
    "- Leave parent_title empty; this is a single ticket, not a batch.\n\n"
    "All human-readable text must be written in SPANISH.\n\n"
    "Output a JSON ARRAY containing exactly ONE ticket object.\n\n" + _TICKET_FIELDS_SPEC
)


class TicketReformatError(RuntimeError):
    """Base class for reformat failures."""


@dataclass
class ReformattedTicket:
    """A proposed rewrite, not yet written back to the tracker."""

    ticket_id: str
    tracker: str
    original_title: str
    original_description: str
    ticket: ProposedTicket
    formatted_title: str
    formatted_description: str


def _fetch(ticket_id: str, tracker: str) -> tuple:
    if tracker == "linear":
        issue = get_linear_issue(ticket_id)
        return issue.get("title") or ticket_id, issue.get("description") or ""
    task = get_clickup_task(ticket_id)
    return task.get("name") or ticket_id, task.get("text_content") or task.get("description") or ""


def render_for(tracker: str, ticket: ProposedTicket) -> str:
    """Render a ticket with the formatter belonging to `tracker`."""
    return format_linear_description(ticket) if tracker == "linear" else format_clickup_description(ticket)


def reformat_ticket(
    ticket_id: str,
    *,
    tracker: str = "clickup",
    timeout_s: Optional[float] = None,
    max_attempts: int = MAX_ATTEMPTS,
    on_step: OnStep = None,
) -> ReformattedTicket:
    """Fetch a ticket and propose a reformatted version of it.

    Read-only — this never writes back. Pass the result to
    apply_reformatted_ticket once a human has approved it.
    """
    if tracker not in TRACKERS:
        raise ValueError(f"Invalid tracker '{tracker}'; must be one of {TRACKERS}")

    label = "Linear" if tracker == "linear" else "ClickUp"
    _report(on_step, f"Fetching ticket {ticket_id} from {label}…")
    original_title, original_description = _fetch(ticket_id, tracker)
    _report(on_step, f'Ticket fetched: "{original_title}".')

    claude_path = _find_claude()
    resolved_timeout = (
        timeout_s if timeout_s is not None else float(os.getenv(CLAUDE_TIMEOUT_ENV_VAR, DEFAULT_CLAUDE_TIMEOUT_S))
    )
    base_prompt = (
        f"{REFORMAT_PROMPT}\n\n=== CURRENT TICKET ===\nTitle: {original_title}\n\n"
        f"{original_description or '(this ticket has no description)'}"
    )

    prompt = base_prompt
    last_error: Optional[str] = None
    for attempt in range(1, max_attempts + 1):
        if last_error is not None:
            prompt = (
                f"{base_prompt}\n\nYour previous response was invalid: {last_error} "
                "Fix this and output ONLY the corrected JSON array with exactly one ticket."
            )
            _report(on_step, f"Fixing an invalid response (attempt {attempt}/{max_attempts})…")
        else:
            _report(on_step, "Restructuring the ticket into the standard format…")

        raw_output = _run_claude(claude_path, prompt, "", resolved_timeout)
        try:
            tickets, _warnings = parse_proposed_tickets(raw_output)
        except TicketParseError as exc:
            last_error = str(exc)
            if attempt == max_attempts:
                raise
            continue

        ticket = tickets[0]
        # Images and attachments are carried across verbatim rather than
        # regenerated: reformatting a ticket must never cost it its
        # screenshots.
        blocks = extract_verbatim_blocks(original_description)
        if blocks:
            ticket = replace(ticket, verbatim_blocks=blocks)
        carried = extract_visual_resources(original_description)
        if carried:
            ticket = replace(ticket, visual_resources=carried)
            _report(on_step, f"Carrying over {len(carried)} image/attachment reference(s) from the original.")
        _report(on_step, "Reformatted — review the proposal before applying it.")
        return ReformattedTicket(
            ticket_id=ticket_id,
            tracker=tracker,
            original_title=original_title,
            original_description=original_description,
            ticket=ticket,
            formatted_title=ticket.title,
            formatted_description=render_for(tracker, ticket),
        )
    raise AssertionError("unreachable")  # loop always returns or raises by the final attempt


def apply_reformatted_ticket(
    ticket_id: str,
    *,
    tracker: str = "clickup",
    title: Optional[str] = None,
    description: Optional[str] = None,
    history: Optional[ReformatHistoryStore] = None,
    on_step: OnStep = None,
) -> dict:
    """Write an approved rewrite back to the tracker.

    Takes the already-reviewed title/description rather than re-running the
    model, so what was approved is exactly what gets written.
    """
    if tracker not in TRACKERS:
        raise ValueError(f"Invalid tracker '{tracker}'; must be one of {TRACKERS}")
    if title is None and description is None:
        raise ValueError("Provide a title and/or a description to update")

    label = "Linear issue" if tracker == "linear" else "ClickUp task"

    # Save what is about to be overwritten BEFORE writing. This is someone
    # else's text; the change has to be undoable.
    history = history if history is not None else ReformatHistoryStore()
    try:
        previous_title, previous_description = _fetch(ticket_id, tracker)
        history.remember(
            ticket_id, tracker,
            title=previous_title, description=previous_description, new_title=title or previous_title,
        )
        _report(on_step, "Saved the current version so this can be undone…")
    except Exception as exc:  # noqa: BLE001 - never block the update over history
        _report(on_step, f"Could not save an undo point ({exc}) — continuing.")

    _report(on_step, f"Updating the {label}…")
    if tracker == "linear":
        updated = update_linear_issue(
            ticket_id, title=title, description=description
        )
    else:
        updated = update_clickup_task(
            ticket_id, name=title, description=description
        )
    _report(on_step, f"{label.capitalize()} updated.")
    return updated


class NoPreviousVersionError(LookupError):
    """No saved pre-reformat version exists for this ticket."""


def revert_ticket(
    ticket_id: str,
    *,
    tracker: str = "clickup",
    history: Optional[ReformatHistoryStore] = None,
    on_step: OnStep = None,
) -> dict:
    """Restore a ticket to the version it had before it was reformatted.

    Writes back the exact title and description that were saved at apply
    time — not a regenerated approximation — then drops the saved version
    so the UI stops offering an undo that has already happened.
    """
    if tracker not in TRACKERS:
        raise ValueError(f"Invalid tracker '{tracker}'; must be one of {TRACKERS}")

    history = history if history is not None else ReformatHistoryStore()
    previous = history.get(ticket_id, tracker)
    if previous is None:
        raise NoPreviousVersionError(
            f"No saved version for {ticket_id} — it has not been reformatted, or it was already reverted."
        )

    label = "Linear issue" if tracker == "linear" else "ClickUp task"
    _report(on_step, f"Restoring the {label} to its previous version…")
    if tracker == "linear":
        updated = update_linear_issue(
            ticket_id, title=previous["title"], description=previous["description"]
        )
    else:
        updated = update_clickup_task(
            ticket_id, name=previous["title"], description=previous["description"]
        )

    history.forget(ticket_id, tracker)
    _report(on_step, f"Restored to the version saved at {previous.get('saved_at', 'an earlier time')}.")
    return updated
