"""Decide whether a ticket actually belongs to a given product module.

The real question this answers is the one a senior asks in review: "check
whether this ticket relates to the People module". Answering it well needs
two things the harness already knows how to get — the ticket's real text
from the tracker, and the module's official documentation supplied as
context — plus a judgement call, which is what the Claude CLI is for.

Same subprocess boundary and retry-with-repair harness as qa_flow.py.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Tuple

from meta_harness.clickup_bridge import get_clickup_task
from meta_harness.linear_bridge import get_linear_issue

OnStep = Optional[Callable[[str], None]]


def _report(on_step: OnStep, message: str) -> None:
    if on_step is not None:
        on_step(message)


TRACKERS = ("clickup", "linear")
VERDICTS = ("related", "partially_related", "unrelated")

CLAUDE_PATH_ENV_VAR = "META_HARNESS_CLAUDE_PATH"
CLAUDE_TIMEOUT_ENV_VAR = "META_HARNESS_MODULE_CHECK_TIMEOUT_S"
DEFAULT_TIMEOUT_S = 180.0
MAX_ATTEMPTS = 3

PROMPT_TEMPLATE = (
    "You are reviewing whether a work ticket belongs to a specific product module. "
    "Below you are given the official documentation describing the module, then the ticket. "
    "Decide, strictly on the evidence given, whether the ticket is work on that module.\n\n"
    "Be conservative: superficially shared vocabulary is not enough. A ticket is only "
    '"related" if the functionality it asks for lives inside the module the documentation '
    'describes. Use "partially_related" when the ticket mainly belongs to another area but '
    "genuinely touches this module (for example it consumes the module's API, or changes a "
    'screen the module owns). Use "unrelated" when the ticket belongs somewhere else.\n\n'
    "Produce a JSON object with exactly these fields: "
    '"verdict" (one of "related", "partially_related", "unrelated"), '
    '"confidence" (number between 0 and 1), '
    '"rationale" (string, 2-4 sentences in Spanish explaining the decision and citing the '
    "specific parts of the documentation and the ticket that drove it), "
    '"matched_aspects" (array of strings, in Spanish — the concrete points where the ticket and '
    "the module documentation genuinely overlap; empty array if none), "
    '"module_gaps" (array of strings, in Spanish — aspects of the ticket that fall OUTSIDE this '
    "module; empty array if none). "
    "Output ONLY the JSON object — no prose, no markdown code fences.\n\n"
    "=== MODULE: {module_name} ===\n{module_context}\n\n"
    "=== TICKET ===\nTitle: {ticket_name}\n\n{ticket_description}"
)


class ModuleRelevanceError(RuntimeError):
    """Base class for module relevance analysis failures."""


class ClaudeNotFoundError(ModuleRelevanceError):
    """No usable `claude` CLI binary could be located."""


class AnalysisGenerationError(ModuleRelevanceError):
    """Raised when invoking the `claude` CLI fails or times out."""


class AnalysisParseError(ModuleRelevanceError):
    """Raised when Claude's output isn't valid JSON or doesn't match the expected shape."""


@dataclass
class ModuleRelevance:
    """The outcome of checking one ticket against one module."""

    ticket_id: str
    ticket_name: str
    module_name: str
    verdict: str
    confidence: float
    rationale: str
    matched_aspects: list
    module_gaps: list

    @property
    def is_related(self) -> bool:
        return self.verdict in ("related", "partially_related")


def _find_claude() -> str:
    env_value = os.getenv(CLAUDE_PATH_ENV_VAR)
    if env_value and Path(env_value).expanduser().exists():
        return str(Path(env_value).expanduser().resolve())
    found = shutil.which("claude")
    if found:
        return found
    raise ClaudeNotFoundError(
        f"No `claude` CLI binary found on PATH. Set {CLAUDE_PATH_ENV_VAR} to its location, "
        "or install/log in to Claude Code."
    )


def _strip_code_fences(raw: str) -> str:
    stripped = raw.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1]
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
    return stripped.strip()


def _string_list(value: object) -> list:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _parse(raw_output: str, ticket_id: str, ticket_name: str, module_name: str) -> ModuleRelevance:
    cleaned = _strip_code_fences(raw_output)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise AnalysisParseError(f"Claude did not return valid JSON: {raw_output[:500]!r}") from exc

    if not isinstance(payload, dict):
        raise AnalysisParseError(f"Expected a JSON object, got: {type(payload).__name__}")

    verdict = payload.get("verdict")
    if verdict not in VERDICTS:
        raise AnalysisParseError(f"Response has invalid verdict {verdict!r}; must be one of {VERDICTS}")

    rationale = payload.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        raise AnalysisParseError("Response missing a valid 'rationale' string")

    confidence = payload.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        confidence = 0.0
    confidence = max(0.0, min(1.0, float(confidence)))

    return ModuleRelevance(
        ticket_id=ticket_id,
        ticket_name=ticket_name,
        module_name=module_name,
        verdict=verdict,
        confidence=confidence,
        rationale=rationale.strip(),
        matched_aspects=_string_list(payload.get("matched_aspects")),
        module_gaps=_string_list(payload.get("module_gaps")),
    )


def fetch_ticket(ticket_id: str, tracker: str, project_path: Optional[Path] = None) -> Tuple[str, str]:
    """Normalize a ticket from either tracker to (name, description)."""
    if tracker not in TRACKERS:
        raise ValueError(f"Invalid tracker '{tracker}'; must be one of {TRACKERS}")
    if tracker == "linear":
        issue = get_linear_issue(ticket_id, project_path=project_path)
        return issue.get("title") or ticket_id, issue.get("description") or ""
    task = get_clickup_task(ticket_id, project_path=project_path)
    return task.get("name") or ticket_id, task.get("text_content") or task.get("description") or ""


def analyze_module_relevance(
    ticket_id: str,
    *,
    tracker: str = "clickup",
    module_name: str,
    module_context: str,
    project_path: Optional[Path] = None,
    timeout_s: Optional[float] = None,
    max_attempts: int = MAX_ATTEMPTS,
    on_step: OnStep = None,
) -> ModuleRelevance:
    """Fetch the ticket from its tracker and judge it against the module docs.

    Read-only: this never writes to either tracker, so it's safe to run
    against anything, including work that is already done.
    """
    if not module_name.strip():
        raise ValueError("module_name is required")
    if not module_context.strip():
        raise ValueError("module_context is required — paste the module's documentation")

    _report(on_step, f"Fetching ticket {ticket_id} from {'Linear' if tracker == 'linear' else 'ClickUp'}…")
    ticket_name, ticket_description = fetch_ticket(ticket_id, tracker, project_path)
    _report(on_step, f'Ticket fetched: "{ticket_name}".')

    claude_path = _find_claude()
    resolved_timeout = (
        timeout_s if timeout_s is not None else float(os.getenv(CLAUDE_TIMEOUT_ENV_VAR, DEFAULT_TIMEOUT_S))
    )
    base_prompt = PROMPT_TEMPLATE.format(
        module_name=module_name.strip(),
        module_context=module_context.strip(),
        ticket_name=ticket_name,
        ticket_description=ticket_description or "(no description)",
    )

    prompt = base_prompt
    last_error: Optional[str] = None
    for attempt in range(1, max_attempts + 1):
        if last_error is not None:
            prompt = (
                f"{base_prompt}\n\nYour previous response was invalid: {last_error} "
                "Fix this and output ONLY the corrected JSON object."
            )
            _report(on_step, f"Fixing an invalid response (attempt {attempt}/{max_attempts})…")
        else:
            _report(on_step, f'Comparing the ticket against the "{module_name}" module…')
        try:
            completed = subprocess.run(
                [claude_path, "-p", prompt, "--output-format", "text"],
                capture_output=True, text=True, timeout=resolved_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise AnalysisGenerationError(f"Claude CLI timed out after {resolved_timeout}s") from exc

        if completed.returncode != 0:
            raise AnalysisGenerationError(f"Claude CLI failed ({completed.returncode}): {completed.stderr.strip()}")

        try:
            result = _parse(completed.stdout, ticket_id, ticket_name, module_name.strip())
        except AnalysisParseError as exc:
            last_error = str(exc)
            if attempt == max_attempts:
                raise
            continue
        _report(on_step, f"Verdict: {result.verdict} (confidence {result.confidence:.0%}).")
        return result
    raise AssertionError("unreachable")  # loop always returns or raises by the final attempt
