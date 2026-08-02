"""The shapes passed between the read side and the write side.

Deliberately inert: no method here calls Linear. A plan is something a human
has looked at and approved, so it has to be inspectable — and serializable —
before anything acts on it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from meta_harness.rework.matching import NameResolution, STATUS_MATCHED


@dataclass(frozen=True)
class WorkflowState:
    """One Linear workflow state."""

    id: str
    name: str
    type: str


@dataclass(frozen=True)
class ParentOption:
    """An issue that could be the parent of the reworked tickets."""

    issue_id: str
    identifier: str
    title: str
    state_name: str = ""


@dataclass
class ReworkPreview:
    """What the read side found, before anything is written.

    Carries the unresolved names as well as the resolved ones: a name nobody
    could match is the single most important thing to show, because it is the
    ticket that will be silently left behind otherwise.
    """

    team_id: str
    resolutions: List[NameResolution] = field(default_factory=list)
    cancelled_state: Optional[WorkflowState] = None
    warnings: List[str] = field(default_factory=list)

    @property
    def matched(self) -> List[NameResolution]:
        return [r for r in self.resolutions if r.status == STATUS_MATCHED and r.chosen]

    @property
    def unresolved(self) -> List[NameResolution]:
        return [r for r in self.resolutions if r.status != STATUS_MATCHED or not r.chosen]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "team_id": self.team_id,
            "cancelled_state": asdict(self.cancelled_state) if self.cancelled_state else None,
            "warnings": list(self.warnings),
            "matched_count": len(self.matched),
            "unresolved_count": len(self.unresolved),
            "resolutions": [
                {
                    "name": resolution.name,
                    "raw": resolution.parsed.raw,
                    "status": resolution.status,
                    "method": resolution.method,
                    "note": resolution.note,
                    "chosen": asdict(resolution.chosen) if resolution.chosen else None,
                    "candidates": [asdict(candidate) for candidate in resolution.candidates],
                }
                for resolution in self.resolutions
            ],
        }


@dataclass(frozen=True)
class ReworkItem:
    """One issue to be reworked."""

    issue_id: str
    identifier: str
    original_title: str


@dataclass
class ReworkPlan:
    """An approved set of issues to rework under one parent."""

    team_id: str
    parent_issue_id: str
    items: List[ReworkItem] = field(default_factory=list)
    cancelled_state_id: str = ""
    # When false the originals are left where they are. Creating the
    # replacements is useful on its own; cancelling is the destructive half and
    # is kept separately switchable.
    cancel_originals: bool = True

    def validate(self) -> None:
        if not self.team_id.strip():
            raise ValueError("team_id is required")
        if not self.parent_issue_id.strip():
            raise ValueError("a parent issue must be selected")
        if not self.items:
            raise ValueError("no issues selected to rework")
        if self.cancel_originals and not self.cancelled_state_id.strip():
            raise ValueError(
                "no cancelled state was resolved for this team — "
                "pick one explicitly or turn off cancelling the originals"
            )
        seen = set()
        for item in self.items:
            if not item.issue_id.strip():
                raise ValueError("every item needs an issue_id")
            if item.issue_id in seen:
                raise ValueError(f"issue {item.identifier or item.issue_id} listed twice")
            seen.add(item.issue_id)
        if self.parent_issue_id in seen:
            # Nesting an issue under itself is rejected by Linear anyway, but
            # failing here names the mistake instead of surfacing a GraphQL error.
            raise ValueError("the parent issue cannot also be one of the reworked issues")


@dataclass
class ReworkOutcome:
    """What happened to one issue."""

    issue_id: str
    identifier: str
    original_title: str
    created_issue_id: str = ""
    created_title: str = ""
    cancelled: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.created_issue_id) and not self.error

    @property
    def needs_attention(self) -> bool:
        """Created but not cancelled — a human has to finish this one."""
        return bool(self.created_issue_id) and not self.cancelled and not self.error

    def to_dict(self) -> Dict[str, Any]:
        return {
            **asdict(self),
            "ok": self.ok,
            "needs_attention": self.needs_attention,
        }


@dataclass
class ReworkReport:
    """The result of executing a plan."""

    parent_issue_id: str
    outcomes: List[ReworkOutcome] = field(default_factory=list)

    @property
    def created(self) -> List[ReworkOutcome]:
        return [outcome for outcome in self.outcomes if outcome.created_issue_id]

    @property
    def failed(self) -> List[ReworkOutcome]:
        return [outcome for outcome in self.outcomes if outcome.error]

    @property
    def stranded(self) -> List[ReworkOutcome]:
        return [outcome for outcome in self.outcomes if outcome.needs_attention]

    def summary_line(self) -> str:
        parts = [f"{len(self.created)}/{len(self.outcomes)} created"]
        if self.stranded:
            parts.append(f"{len(self.stranded)} original(s) still open")
        if self.failed:
            parts.append(f"{len(self.failed)} failed")
        return ", ".join(parts)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "parent_issue_id": self.parent_issue_id,
            "summary": self.summary_line(),
            "created_count": len(self.created),
            "failed_count": len(self.failed),
            "stranded_count": len(self.stranded),
            "outcomes": [outcome.to_dict() for outcome in self.outcomes],
        }
