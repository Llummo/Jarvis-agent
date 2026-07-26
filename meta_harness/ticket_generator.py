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
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

import pypdf

CLAUDE_PATH_ENV_VAR = "META_HARNESS_CLAUDE_PATH"
CLAUDE_TIMEOUT_ENV_VAR = "META_HARNESS_CLAUDE_TIMEOUT_S"
DEFAULT_CLAUDE_TIMEOUT_S = 180.0

PRIORITIES = ("urgent", "high", "normal", "low")
DEFAULT_PRIORITY = "normal"

SUPPORTED_EXTENSIONS = (".pdf", ".txt", ".md")

TICKET_EXTRACTION_PROMPT = (
    "Extract a JSON array of tickets from the requirements document provided via stdin. "
    'Each ticket must be a JSON object with exactly these fields: '
    '"title" (string), "description" (string), '
    '"acceptance_criteria" (array of strings), '
    '"priority" (one of: "urgent", "high", "normal", "low"). '
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
    acceptance_criteria: List[str] = field(default_factory=list)
    priority: str = DEFAULT_PRIORITY


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

    return ProposedTicket(
        title=title.strip(),
        description=description,
        acceptance_criteria=acceptance_criteria,
        priority=priority,
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


def generate_tickets_from_text(
    document_text: str, *, timeout_s: Optional[float] = None
) -> Tuple[List[ProposedTicket], List[str]]:
    """Run `claude -p <prompt>` with the document piped via stdin; parse the result."""
    claude_path = _find_claude()
    resolved_timeout = (
        timeout_s if timeout_s is not None else float(os.getenv(CLAUDE_TIMEOUT_ENV_VAR, DEFAULT_CLAUDE_TIMEOUT_S))
    )
    command = [claude_path, "-p", TICKET_EXTRACTION_PROMPT, "--output-format", "text"]
    try:
        completed = subprocess.run(
            command, input=document_text, capture_output=True, text=True, timeout=resolved_timeout
        )
    except subprocess.TimeoutExpired as exc:
        raise TicketGenerationError(
            f"Claude CLI timed out after {resolved_timeout}s analyzing the document"
        ) from exc

    if completed.returncode != 0:
        raise TicketGenerationError(f"Claude CLI failed ({completed.returncode}): {completed.stderr.strip()}")

    return parse_proposed_tickets(completed.stdout)


def generate_tickets_from_file(
    filename: str, content: bytes, *, timeout_s: Optional[float] = None
) -> Tuple[List[ProposedTicket], List[str]]:
    """End-to-end: extract text from an uploaded file, then generate tickets."""
    text = extract_document_text(filename, content)
    return generate_tickets_from_text(text, timeout_s=timeout_s)
