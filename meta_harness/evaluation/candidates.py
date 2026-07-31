"""The settings being compared when one run is judged against another.

A candidate is the independent variable. Everything else in an evaluation — the
tickets, their labels, the module — is held fixed, so that a difference in score
can be attributed to something. Upstream a candidate is a Python file that
configures hermes-agent; here it is the handful of knobs that plausibly change
whether this harness judges a ticket correctly.

Each knob is already a real parameter of the code being measured, not a new
setting invented for the benchmark. A variant that cannot be expressed as a call
to `retrieve_module_context` and `analyze_module_relevance` would be measuring a
harness nobody runs.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Any, Dict, Optional, Sequence

from meta_harness.module_relevance import DEFAULT_CONTEXT_CHUNKS

# Retrieving more chunks is not free in either direction: too few and the judge
# has no evidence, too many and the module's real boundary is buried in loosely
# related code. The point of the loop is to find where that sits, so the range
# only needs to be wide enough to contain the answer.
MIN_CONTEXT_LIMIT = 1
MAX_CONTEXT_LIMIT = 60


class CandidateError(ValueError):
    """Raised when a candidate's settings are invalid or contradictory."""


@dataclass(frozen=True)
class Candidate:
    """One configuration of the harness, under a name it can be reported by."""

    name: str
    # How many retrieved spans describe the module.
    context_limit: int = DEFAULT_CONTEXT_CHUNKS
    # Whether spans that no longer match the file on disk are dropped. Turning
    # this off trades trustworthy context for more of it — worth measuring, and
    # exactly the sort of thing that is argued about without evidence.
    verified_only: bool = True
    # Text placed above the module context, framing how it should be read.
    context_preamble: str = ""
    timeout_s: Optional[float] = None

    def validate(self) -> None:
        if not self.name.strip():
            raise CandidateError("a candidate needs a name")
        if not MIN_CONTEXT_LIMIT <= self.context_limit <= MAX_CONTEXT_LIMIT:
            raise CandidateError(
                f"{self.name}: context_limit {self.context_limit} outside "
                f"{MIN_CONTEXT_LIMIT}–{MAX_CONTEXT_LIMIT}"
            )
        if self.timeout_s is not None and self.timeout_s <= 0:
            raise CandidateError(f"{self.name}: timeout_s must be positive")

    def render_context(self, module_context: str) -> str:
        """Apply the preamble to retrieved context.

        Kept separate from retrieval so the same retrieved spans can be framed
        several ways without paying for the search again — and so a difference
        in score between two framings is not confounded by a different search.
        """
        body = module_context.strip()
        preamble = self.context_preamble.strip()
        if not preamble:
            return body
        return f"{preamble}\n\n{body}" if body else preamble

    def fingerprint(self) -> str:
        """Identity of the settings, ignoring the name.

        Mutation generates variants mechanically and will sooner or later emit
        two that differ only in name. Scoring both wastes a run and, worse,
        makes a tie look like a reproducible result.
        """
        payload = json.dumps(
            {
                "context_limit": self.context_limit,
                "verified_only": self.verified_only,
                "context_preamble": self.context_preamble.strip(),
                "timeout_s": self.timeout_s,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def describe(self) -> Dict[str, Any]:
        """What went into the manifest, so a run can be reproduced later."""
        return {
            "name": self.name,
            "context_limit": self.context_limit,
            "verified_only": self.verified_only,
            "context_preamble": self.context_preamble,
            "timeout_s": self.timeout_s,
            "fingerprint": self.fingerprint(),
        }

    def variant(self, name: str, **changes: Any) -> "Candidate":
        """A copy with some settings changed. The basis for mutation."""
        candidate = replace(self, name=name, **changes)
        candidate.validate()
        return candidate

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "Candidate":
        if not isinstance(payload, dict):
            raise CandidateError(f"expected an object, got {type(payload).__name__}")
        known = {"name", "context_limit", "verified_only", "context_preamble", "timeout_s"}
        unknown = set(payload) - known - {"fingerprint"}
        if unknown:
            raise CandidateError(f"unknown candidate fields: {sorted(unknown)}")
        timeout = payload.get("timeout_s")
        candidate = cls(
            name=str(payload.get("name", "")),
            context_limit=int(payload.get("context_limit", DEFAULT_CONTEXT_CHUNKS)),
            verified_only=bool(payload.get("verified_only", True)),
            context_preamble=str(payload.get("context_preamble", "")),
            timeout_s=float(timeout) if timeout is not None else None,
        )
        candidate.validate()
        return candidate


def baseline() -> Candidate:
    """The harness exactly as it ships.

    Every comparison needs this. A candidate that scores 70% is neither good nor
    bad until it is set against what the current code already does.
    """
    return Candidate(name="baseline")


def dedupe(candidates: Sequence[Candidate]) -> list:
    """Drop candidates whose settings duplicate an earlier one."""
    seen = set()
    unique = []
    for candidate in candidates:
        fingerprint = candidate.fingerprint()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        unique.append(candidate)
    return unique
