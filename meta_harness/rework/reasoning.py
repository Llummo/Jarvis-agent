"""Ask a model which candidate a name meant, when scoring could not tell.

Reserved for the cases string similarity genuinely cannot settle: a name
abbreviated past recognition, two issues differing only in a word the scorer
treats as noise. Everything scoring can decide is decided without a model,
because a model here costs a subprocess and a wait per name.

The model is given a closed choice — pick one of these candidates or decline —
and never asked to produce a title. Letting it write one would invent an issue
that does not exist, which is the one failure this whole flow exists to avoid.
"""

from __future__ import annotations

import json
import os
import re
from typing import Optional, Sequence

from meta_harness.rework.matching import IssueMatch, ParsedName
from meta_harness.ticket_generator import (
    CLAUDE_TIMEOUT_ENV_VAR,
    ClaudeNotFoundError,
    TicketGenerationError,
    _find_claude,
    _run_claude,
    _strip_code_fences,
)

# Picking one of five short titles is a small question; the document-sized
# default would keep a stuck call alive for half an hour.
DEFAULT_REASONING_TIMEOUT_S = 120.0

PROMPT = (
    "You are matching a ticket name, copied from a chat message, to the real "
    "issue it refers to.\n\n"
    "You will receive JSON with a `name` and a list of `candidates`. Decide "
    "which candidate the name refers to.\n\n"
    "Rules:\n"
    "- The name is often abbreviated, misspelled, or missing accents. Judge by "
    "meaning, not by string similarity.\n"
    "- A leading section number (for example 6.5 or 1.16) is the team's own "
    "numbering. If both the name and a candidate carry one and they differ, "
    "they are different tickets, however similar the words are.\n"
    "- If no candidate is the same ticket, decline. A wrong match silently "
    "rewrites the wrong issue, which is far worse than leaving one unresolved.\n\n"
    'Reply with JSON only: {"issue_id": "<id>", "confidence": 0.0-1.0, "why": "<one sentence>"} '
    'or {"issue_id": null, "why": "<one sentence>"}.'
)

# Below this the model is not confident enough to act on unreviewed.
MIN_CONFIDENCE = 0.6


def _timeout() -> float:
    raw = os.getenv(CLAUDE_TIMEOUT_ENV_VAR)
    if not raw:
        return DEFAULT_REASONING_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_REASONING_TIMEOUT_S
    return value if value > 0 else DEFAULT_REASONING_TIMEOUT_S


def _payload(parsed: ParsedName, candidates: Sequence[IssueMatch]) -> str:
    return json.dumps(
        {
            "name": parsed.title,
            "numeric_prefix": parsed.numeric_prefix,
            "candidates": [
                {
                    "issue_id": candidate.issue_id,
                    "identifier": candidate.identifier,
                    "title": candidate.title,
                    "similarity": candidate.score,
                }
                for candidate in candidates
            ],
        },
        ensure_ascii=False,
        indent=2,
    )


def _parse_choice(raw: str, allowed: Sequence[str]) -> Optional[str]:
    text = _strip_code_fences(raw).strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        # The CLI occasionally wraps JSON in prose; take the first object.
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None

    if not isinstance(payload, dict):
        return None
    issue_id = payload.get("issue_id")
    if not issue_id or not isinstance(issue_id, str):
        return None

    confidence = payload.get("confidence")
    if isinstance(confidence, (int, float)) and confidence < MIN_CONFIDENCE:
        return None
    # Only ever return an id that was actually offered — the model must choose,
    # not invent.
    return issue_id if issue_id in set(allowed) else None


def claude_reasoner(
    parsed: ParsedName, candidates: Sequence[IssueMatch]
) -> Optional[str]:
    """Pick one candidate's issue id, or None to leave the name unresolved.

    Never raises: a model that is missing, slow, or incoherent leaves the name
    ambiguous for a human to settle, which the UI already handles.
    """
    if not candidates:
        return None
    try:
        claude_path = _find_claude()
        raw = _run_claude(claude_path, PROMPT, _payload(parsed, candidates), _timeout())
    except (ClaudeNotFoundError, TicketGenerationError, OSError):
        return None
    return _parse_choice(raw, [candidate.issue_id for candidate in candidates])
