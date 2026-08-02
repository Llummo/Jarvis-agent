"""The read side. Nothing in this module writes to Linear.

That is the whole point of splitting it out: preview is the step a human uses
to decide, so it has to be safe to run repeatedly, on the wrong team, with a
badly pasted list, without consequence. The import list is the guarantee —
only read functions are in scope here, so a write cannot be added by accident.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Sequence

from meta_harness.linear_bridge import (
    get_linear_issue,
    list_linear_issue_index,
    list_linear_states,
    search_linear_issues,
)
from meta_harness.rework.matching import (
    Reasoner,
    parse_pasted_names,
    resolve_names,
)
from meta_harness.rework.models import ParentOption, ReworkPreview, WorkflowState

OnStep = Optional[Callable[[str], None]]

# Linear's state machine calls the terminal-rejected column "canceled". Teams
# rename the display label freely, so the type is what is matched on, with the
# names kept only as a fallback for a workflow that has been customized oddly.
CANCELLED_STATE_TYPE = "canceled"
CANCELLED_NAME_HINTS = ("cancel", "cancelad", "descartad", "anulad")


def _report(on_step: OnStep, message: str) -> None:
    if on_step is not None:
        on_step(message)


def list_workflow_states(team_id: str) -> List[WorkflowState]:
    return [
        WorkflowState(
            id=str(state.get("id") or ""),
            name=str(state.get("name") or ""),
            type=str(state.get("type") or ""),
        )
        for state in list_linear_states(team_id)
    ]


def find_cancelled_state(team_id: str) -> Optional[WorkflowState]:
    """The state an obsolete issue should be moved to, or None.

    None is a legitimate answer — a team may simply not have such a column —
    and the caller must handle it rather than guessing at a substitute.
    """
    states = list_workflow_states(team_id)
    for state in states:
        if state.type == CANCELLED_STATE_TYPE:
            return state
    lowered = [(state, state.name.lower()) for state in states]
    for state, name in lowered:
        if any(hint in name for hint in CANCELLED_NAME_HINTS):
            return state
    return None


def _to_option(issue: Dict) -> ParentOption:
    return ParentOption(
        issue_id=str(issue.get("id") or ""),
        identifier=str(issue.get("identifier") or ""),
        title=str(issue.get("title") or ""),
        state_name=str((issue.get("state") or {}).get("name") or ""),
    )


def list_parent_options(team_id: str, *, limit: Optional[int] = None) -> List[ParentOption]:
    """Every issue on the team, offered as possible parents.

    Backed by the title-only index, so listing a whole team is cheap enough to
    do up front. A dropdown you can open and read beats a search box that
    stays empty until you guess a word that happens to match.
    """
    return [_to_option(issue) for issue in list_linear_issue_index(team_id, limit=limit)]


def find_parent_candidates(team_id: str, text: str, *, limit: int = 25) -> List[ParentOption]:
    """Issues matching `text`, offered as possible parents.

    Filtered by Linear rather than locally: the caller is typing, so the query
    is specific and the round trip is cheaper than pulling the team down.
    """
    return [_to_option(issue) for issue in search_linear_issues(team_id, text, limit=limit)]


def get_parent_option(issue_id: str) -> ParentOption:
    """One issue, addressed directly — for a parent pasted rather than searched."""
    return _to_option(get_linear_issue(issue_id))


def preview_rework(
    team_id: str,
    pasted_names: str,
    *,
    reasoner: Optional[Reasoner] = None,
    issues: Optional[Sequence[Dict]] = None,
    on_step: OnStep = None,
) -> ReworkPreview:
    """Resolve a pasted list of names against the team, changing nothing.

    `issues` is injectable so a caller holding a fresh index — or a test — can
    skip the fetch; left unset it is loaded once and reused for every name.
    """
    parsed = parse_pasted_names(pasted_names)
    warnings: List[str] = []
    if not parsed:
        warnings.append("No ticket names were found in the pasted text.")

    if issues is None:
        _report(on_step, "Loading the team's issue titles…")
        issues = list_linear_issue_index(team_id)
    _report(on_step, f"Matching {len(parsed)} name(s) against {len(issues)} issue(s)…")

    resolutions = resolve_names(parsed, issues, reasoner=reasoner)

    cancelled_state = find_cancelled_state(team_id)
    if cancelled_state is None:
        warnings.append(
            "This team has no cancelled state, so the originals cannot be moved "
            "out of the way automatically."
        )

    unresolved = [r for r in resolutions if r.status != "matched" or not r.chosen]
    if unresolved:
        warnings.append(
            f"{len(unresolved)} of {len(resolutions)} name(s) could not be matched confidently."
        )

    duplicates = _duplicate_targets(resolutions)
    if duplicates:
        warnings.append(
            "Two or more names resolved to the same issue: "
            + ", ".join(sorted(duplicates))
            + ". Reworking it twice would create duplicate sub-issues."
        )

    _report(on_step, f"Matched {len(resolutions) - len(unresolved)} of {len(resolutions)} name(s).")
    return ReworkPreview(
        team_id=team_id,
        resolutions=resolutions,
        cancelled_state=cancelled_state,
        warnings=warnings,
    )


def _duplicate_targets(resolutions: Sequence) -> List[str]:
    seen: Dict[str, int] = {}
    for resolution in resolutions:
        if resolution.chosen:
            key = resolution.chosen.identifier or resolution.chosen.issue_id
            seen[key] = seen.get(key, 0) + 1
    return [key for key, count in seen.items() if count > 1]
