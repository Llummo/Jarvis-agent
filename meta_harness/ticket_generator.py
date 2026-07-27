"""Turn a requirements document into a list of proposed ClickUp tickets by
shelling out to the local, already-authenticated Claude Code CLI.

Kept as a subprocess boundary, same rationale as clickup_bridge.py: this
module never touches an LLM API key directly — it drives the `claude`
binary that's already installed and logged in on this machine.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import pypdf

OnStep = Optional[Callable[[str], None]]


def _report(on_step: OnStep, message: str) -> None:
    if on_step is not None:
        on_step(message)

CLAUDE_PATH_ENV_VAR = "META_HARNESS_CLAUDE_PATH"
CLAUDE_TIMEOUT_ENV_VAR = "META_HARNESS_CLAUDE_TIMEOUT_S"
DEFAULT_CLAUDE_TIMEOUT_S = 300.0

PRIORITIES = ("urgent", "high", "normal", "low")
DEFAULT_PRIORITY = "normal"

CATEGORIES = ("mundane", "backend", "frontend", "deployment")
DEFAULT_CATEGORY = "mundane"
CATEGORY_PREFIXES = {
    "mundane": "TAM",
    "backend": "TAB",
    "frontend": "TAF",
    "deployment": "TAD",
}

SUPPORTED_EXTENSIONS = (".pdf", ".txt", ".md")

SPRINT_LENGTH_DAYS = 28  # Scrum: fixed 4-week sprints

TICKET_EXTRACTION_PROMPT = (
    "Extract a JSON array of tickets from the requirements document provided via stdin. "
    "Decompose the document into specific, well-scoped tickets. Size each ticket so one person "
    "could complete it in roughly half a day to two days of focused work: if a piece of work "
    "described would take much longer than that, split it into smaller tickets; if a step "
    "would only take a few minutes on its own, fold it into the ticket it belongs to instead "
    'of giving it a separate ticket. For example, "build the login page" is too broad for one '
    "ticket — split it into the login form UI and the authentication API call — but a single "
    "form field or one validation rule within that same feature is too small to be its own "
    "ticket. If a requirement bundles several distinct pieces of functionality (several CRUD "
    "operations, several pages or views, several independent endpoints), give each one that "
    "stands on its own its own ticket, sized as above. Let the document's actual scope decide "
    "the count — a short document should produce few tickets, a larger one naturally produces "
    "more; do not pad the list to hit any particular number. "
    "Order the array in a logical implementation sequence — foundational or setup work and "
    "anything another ticket depends on comes before the tickets that build on it (e.g. a "
    "backend endpoint before the frontend that calls it, setup/config before the feature that "
    "needs it). "
    'Each ticket must be a JSON object with exactly these fields: '
    '"title" (string, short — a few words naming the task; do not add numbering or a prefix, '
    "that is added separately), "
    '"user_story" (string, in English, following exactly this template: "As a <role>, I want '
    'to <goal>, so I can <benefit>." — pick whichever role best fits the document\'s context, '
    'e.g. "user", "admin", "student", "developer"), '
    '"description" (string — a brief, plain-language explanation of the core of the task and '
    "its flow: what needs to be built and how it should work end to end, written so anyone "
    "picking up the ticket immediately understands it without re-reading the source document; "
    "distinct from and more concrete than the user_story above), "
    '"acceptance_criteria" (array of strings, each written in Gherkin: "Given <context>, when '
    '<action>, then <expected outcome>"), '
    '"priority" (one of: "urgent", "high", "normal", "low"), '
    '"category" (one of: "backend", "frontend", "deployment", "mundane" — classify by the '
    'primary type of work: "backend" for server/API/database/business-logic work, '
    '"frontend" for UI/client-side work, "deployment" for CI/CD, infra, release, or ops work, '
    'and "mundane" for anything general, cross-cutting, or planning-oriented that does not '
    "clearly fit the other three), "
    '"sprint" (integer, 1 or higher — which 4-week Scrum sprint this ticket is planned for. '
    "Reason about this like a sprint-planning session: work through the backlog in priority "
    "and dependency order, and don't overload a single sprint — a small team can realistically "
    "finish somewhere around 8-15 right-sized tickets in one 4-week sprint, fewer if they're "
    "larger or more complex, so once a sprint is full move on to the next one. A ticket can "
    "never be scheduled in an earlier sprint than a ticket it depends on). "
    "Output ONLY the JSON array — no prose, no markdown code fences, no explanation."
)


class TicketExtractionError(RuntimeError):
    """Raised when document text extraction fails (bad/unsupported file, corrupt PDF)."""


class ClaudeNotFoundError(RuntimeError):
    """No usable `claude` CLI binary could be located."""


class TicketGenerationError(RuntimeError):
    """Raised when invoking the `claude` CLI fails or times out."""


class TicketParseError(RuntimeError):
    """Raised when Claude's output isn't valid JSON or contains no usable tickets."""


@dataclass
class ProposedTicket:
    """One LLM-proposed ticket, not yet created anywhere."""

    title: str
    description: str
    user_story: str = ""
    acceptance_criteria: List[str] = field(default_factory=list)
    priority: str = DEFAULT_PRIORITY
    category: str = DEFAULT_CATEGORY
    sprint: int = 1
    due_date: Optional[str] = None  # ISO date, set by apply_sprint_due_dates
    assignee_user_id: Optional[int] = None
    assignee_email: Optional[str] = None
    assignee_name: Optional[str] = None


def extract_document_text(filename: str, content: bytes) -> str:
    """Extract plain text from a .pdf/.txt/.md upload's raw bytes."""
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf":
        return _extract_pdf_text(content)
    if suffix in (".txt", ".md"):
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise TicketExtractionError(f"Could not decode '{filename}' as UTF-8 text") from exc
    raise TicketExtractionError(
        f"Unsupported file type '{suffix or '(none)'}' for '{filename}'. "
        f"Supported: {', '.join(SUPPORTED_EXTENSIONS)}"
    )


def _extract_pdf_text(content: bytes) -> str:
    try:
        reader = pypdf.PdfReader(io.BytesIO(content))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as exc:  # noqa: BLE001 - surfaced as a typed extraction error
        raise TicketExtractionError(f"Could not parse PDF: {exc}") from exc
    if not text.strip():
        raise TicketExtractionError("PDF contained no extractable text")
    return text


def _find_claude() -> str:
    """Locate the `claude` CLI binary: env var override, then PATH."""
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
    """Defensively strip ```json ... ``` fences in case Claude ignores the
    "no markdown fences" instruction — cheap and harmless if absent."""
    stripped = raw.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1]
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
    return stripped.strip()


def _validate_ticket(raw: dict, index: int, warnings: List[str]) -> Optional[ProposedTicket]:
    """Validate one raw ticket dict leniently.

    Only a missing/invalid title drops the ticket entirely (title becomes
    the ClickUp task name — there's no sane default). Every other field is
    coerced to a safe default with a warning, since the review step in the
    UI is exactly where a partially-defaulted ticket gets caught or fixed.
    """
    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        warnings.append(f"Ticket #{index}: dropped — missing or invalid 'title'")
        return None

    description = raw.get("description")
    if not isinstance(description, str):
        warnings.append(f"Ticket #{index} ('{title}'): missing/invalid 'description', defaulted to empty")
        description = ""

    user_story = raw.get("user_story")
    if not isinstance(user_story, str) or not user_story.strip():
        warnings.append(f"Ticket #{index} ('{title}'): missing/invalid 'user_story', defaulted to empty")
        user_story = ""

    acceptance_criteria = raw.get("acceptance_criteria")
    if not isinstance(acceptance_criteria, list):
        warnings.append(
            f"Ticket #{index} ('{title}'): missing/invalid 'acceptance_criteria', defaulted to []"
        )
        acceptance_criteria = []
    else:
        cleaned = [item for item in acceptance_criteria if isinstance(item, str)]
        if len(cleaned) != len(acceptance_criteria):
            warnings.append(f"Ticket #{index} ('{title}'): dropped non-string acceptance_criteria entries")
        acceptance_criteria = cleaned

    priority = raw.get("priority")
    if priority not in PRIORITIES:
        warnings.append(
            f"Ticket #{index} ('{title}'): invalid priority {priority!r}, defaulted to '{DEFAULT_PRIORITY}'"
        )
        priority = DEFAULT_PRIORITY

    category = raw.get("category")
    if category not in CATEGORIES:
        warnings.append(
            f"Ticket #{index} ('{title}'): invalid category {category!r}, defaulted to '{DEFAULT_CATEGORY}'"
        )
        category = DEFAULT_CATEGORY

    sprint = raw.get("sprint")
    if not isinstance(sprint, int) or isinstance(sprint, bool) or sprint < 1:
        warnings.append(f"Ticket #{index} ('{title}'): missing/invalid 'sprint', defaulted to 1")
        sprint = 1

    return ProposedTicket(
        title=title.strip(),
        description=description,
        user_story=user_story.strip(),
        acceptance_criteria=acceptance_criteria,
        priority=priority,
        category=category,
        sprint=sprint,
    )


def parse_proposed_tickets(raw_output: str) -> Tuple[List[ProposedTicket], List[str]]:
    """Parse and validate Claude's JSON output into (tickets, warnings).

    Whole-batch rejection (TicketParseError) only happens when the output
    isn't parseable JSON at all, isn't a JSON array, or every single item
    fails validation — i.e. when nothing usable survived.
    """
    cleaned = _strip_code_fences(raw_output)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise TicketParseError(f"Claude did not return valid JSON: {raw_output[:500]!r}") from exc

    if not isinstance(payload, list):
        raise TicketParseError(f"Expected a JSON array of tickets, got: {type(payload).__name__}")

    warnings: List[str] = []
    tickets: List[ProposedTicket] = []
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            warnings.append(f"Ticket #{index}: dropped — not a JSON object")
            continue
        ticket = _validate_ticket(item, index, warnings)
        if ticket is not None:
            tickets.append(ticket)

    if not tickets:
        raise TicketParseError("No usable tickets could be extracted from Claude's output")

    return tickets, warnings


MAX_GENERATION_ATTEMPTS = 3


def _run_claude(claude_path: str, prompt: str, document_text: str, timeout_s: float) -> str:
    command = [claude_path, "-p", prompt, "--output-format", "text"]
    try:
        completed = subprocess.run(
            command, input=document_text, capture_output=True, text=True, timeout=timeout_s
        )
    except subprocess.TimeoutExpired as exc:
        raise TicketGenerationError(f"Claude CLI timed out after {timeout_s}s analyzing the document") from exc

    if completed.returncode != 0:
        raise TicketGenerationError(f"Claude CLI failed ({completed.returncode}): {completed.stderr.strip()}")

    return completed.stdout


def generate_tickets_from_text(
    document_text: str,
    *,
    timeout_s: Optional[float] = None,
    max_attempts: int = MAX_GENERATION_ATTEMPTS,
    on_step: OnStep = None,
) -> Tuple[List[ProposedTicket], List[str]]:
    """Run `claude -p <prompt>` with the document piped via stdin; parse the result.

    This is the harness around the model call: if Claude's output fails
    JSON/shape validation, the specific parse error is fed back into a
    follow-up prompt asking it to self-correct, up to `max_attempts`
    total tries, instead of failing the whole batch on the first bad
    response. A malformed-but-fixable response is far more common than a
    genuinely bad document, so this materially improves real output
    quality without masking a document that's truly unusable.
    """
    claude_path = _find_claude()
    resolved_timeout = (
        timeout_s if timeout_s is not None else float(os.getenv(CLAUDE_TIMEOUT_ENV_VAR, DEFAULT_CLAUDE_TIMEOUT_S))
    )

    prompt = TICKET_EXTRACTION_PROMPT
    last_error: Optional[str] = None
    for attempt in range(1, max_attempts + 1):
        if last_error is not None:
            prompt = (
                f"{TICKET_EXTRACTION_PROMPT}\n\nYour previous response was invalid: {last_error} "
                "Fix this and output ONLY the corrected JSON array."
            )
            _report(
                on_step,
                f"Claude's response didn't pass validation ({last_error}) — "
                f"asking it to fix and retrying (attempt {attempt}/{max_attempts})…",
            )
        else:
            _report(on_step, "Sending document to Claude for ticket extraction — this can take a minute…")
        raw_output = _run_claude(claude_path, prompt, document_text, resolved_timeout)
        try:
            tickets, warnings = parse_proposed_tickets(raw_output)
        except TicketParseError as exc:
            last_error = str(exc)
            if attempt == max_attempts:
                raise
            continue
        _report(on_step, f"Extracted {len(tickets)} ticket(s) from the document.")
        return tickets, warnings
    raise AssertionError("unreachable")  # loop always returns or raises by the final attempt


def generate_tickets_from_file(
    filename: str, content: bytes, *, timeout_s: Optional[float] = None, on_step: OnStep = None
) -> Tuple[List[ProposedTicket], List[str]]:
    """End-to-end: extract text from an uploaded file, then generate tickets."""
    _report(on_step, f'Extracting text from "{filename}"…')
    text = extract_document_text(filename, content)
    return generate_tickets_from_text(text, timeout_s=timeout_s, on_step=on_step)


def apply_category_numbering(
    tickets: List[ProposedTicket], start_numbers: Optional[Dict[str, int]] = None
) -> List[ProposedTicket]:
    """Rename tickets in order using a per-category prefix and counter:
    TAM (mundane), TAB (backend), TAF (frontend), TAD (deployment).

    Each category counts independently in the order tickets appear, so an
    existing sequence (e.g. TAM already at 01) can be continued via
    `start_numbers={"mundane": 2}` without disturbing the other three,
    each of which still starts at 1 by default. Returns a new list — the
    input tickets are not mutated.
    """
    counters: Dict[str, int] = dict(start_numbers or {})
    numbered = []
    for ticket in tickets:
        category = ticket.category if ticket.category in CATEGORIES else DEFAULT_CATEGORY
        prefix = CATEGORY_PREFIXES[category]
        number = counters.get(category, 1)
        counters[category] = number + 1
        numbered.append(replace(ticket, title=f"{prefix}-{number:02d} | {ticket.title}"))
    return numbered


def apply_sprint_due_dates(
    tickets: List[ProposedTicket], *, sprint_start: Optional[date] = None
) -> List[ProposedTicket]:
    """Convert each ticket's LLM-assigned `sprint` number into a concrete
    due date: a ticket is due at the end of its assigned 4-week sprint,
    counted from sprint_start (today by default). Returns a new list —
    the input tickets are not mutated.
    """
    start = sprint_start if sprint_start is not None else date.today()
    dated = []
    for ticket in tickets:
        sprint = ticket.sprint if ticket.sprint >= 1 else 1
        due = start + timedelta(days=SPRINT_LENGTH_DAYS * sprint)
        dated.append(replace(ticket, due_date=due.isoformat()))
    return dated
