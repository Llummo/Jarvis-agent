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
import re
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
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
# Generation is output-bound: a large requirements document produces 50-80
# fully-structured tickets, which is tens of thousands of tokens to emit.
# A real 24k-token statement blew past the previous 600s ceiling, so this is
# sized for the worst realistic document rather than the typical one.
# Override per-environment with META_HARNESS_CLAUDE_TIMEOUT_S.
DEFAULT_CLAUDE_TIMEOUT_S = 1800.0

PRIORITIES = ("urgent", "high", "normal", "low")
DEFAULT_PRIORITY = "normal"

CATEGORIES = ("mundane", "backend", "frontend", "deployment")
DEFAULT_CATEGORY = "mundane"
# One prefix for every ticket. The old per-category prefixes (TAB/TAF/TAD/TAM)
# claimed a distinction the model could not make reliably: a technical story
# and a user story are told apart by what they describe, not by a label the
# generator guesses. Category is still classified for the UI badge, but it no
# longer drives the identifier.
TICKET_PREFIX = "TAS"

SUPPORTED_EXTENSIONS = (".pdf", ".txt", ".md")

SPRINT_LENGTH_DAYS = 28  # Scrum: fixed 4-week sprints

_PROMPT_HEAD = (
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
    "stands on its own its own ticket, sized as above. "
)

# How many tickets to aim for. Swapped out per section when a large document
# is split, so each call is asked for its own share rather than the whole
# document's 50-80 (which would multiply the total by the number of sections).
_COUNT_GUIDANCE_WHOLE = (
    "Let the document's actual scope decide "
    "the count — a short document should produce few tickets, a larger one naturally produces "
    "more; do not pad the list to hit any particular number. But even for a large, "
    "multi-feature document, keep the total count roughly in the 50-80 range — if strict "
    "half-day-to-two-day sizing would push noticeably past that, favor slightly broader (but "
    "still coherent and specific) ticket scopes instead of fragmenting further, rather than "
    "letting the total run unbounded. "
)


def _count_guidance_for_document(low: int, high: int) -> str:
    return (
        "Let the document's actual scope decide the count; do not pad the list to hit any "
        f"particular number. For a document of this size aim for roughly {low}-{high} tickets — "
        "if strict half-day-to-two-day sizing would push noticeably past that, favor slightly "
        "broader (but still coherent and specific) ticket scopes instead of fragmenting further. "
    )


def _count_guidance_for_chunk(low: int, high: int) -> str:
    return (
        "Let this section's actual scope decide the count; do not pad the list to hit any "
        f"particular number. Aim for roughly {low}-{high} tickets for this section — if strict "
        "half-day-to-two-day sizing would push noticeably past that, favor slightly broader (but "
        "still coherent and specific) ticket scopes instead of fragmenting further. "
    )


# What separates a technical story from a user story. Shared by every prompt
# via _TICKET_FIELDS_SPEC, so generation, chat and reformat all apply it.
_STORY_KIND_RULES = (
    "EVERY ticket is one of two kinds, and the kind decides what its body must contain:\n"
    "- A TECHNICAL STORY (TS) describes server-side capability: an endpoint, a job, a migration, "
    "a piece of infrastructure. Its value is the contract, not a screen. For one of these you MUST "
    'fill "backend_endpoint" with the concrete method(s) and path (e.g. '
    '"POST /api/v1/talent/people"), leave "ui_route" empty unless a screen is genuinely in scope, '
    "and write the description and acceptance criteria in terms of the endpoint's behaviour: what "
    "it receives, what it validates, what it returns on success, and what it returns on each "
    "failure (status codes, error shapes, permissions, side effects). Do NOT write a technical "
    'story as though a person were clicking through a screen, and do not leave "backend_endpoint" '
    "empty on a ticket whose whole purpose is server-side work.\n"
    "- A USER STORY (US) describes a need a person has and the flow that satisfies it. For one of "
    'these fill "ui_route" with the screen(s) involved, and write the description and criteria in '
    "terms of what the user sees and does. Reference the endpoint it consumes when there is one, "
    "but the subject is the flow.\n"
    "Decide the kind from what the work actually is. If a piece of work has both a server side "
    "and a screen, split it into one technical story and one user story rather than blurring both "
    "into a single ticket.\n"
    "\n"
)


_PROMPT_TAIL = (
    "Order the array in a logical implementation sequence — foundational or setup work and "
    "anything another ticket depends on comes before the tickets that build on it (e.g. a "
    "backend endpoint before the frontend that calls it, setup/config before the feature that "
    "needs it). "
    "\n\n"
    + _STORY_KIND_RULES
    + "All human-readable text content in the output (title, epic, user_story, description, "
    "acceptance_criteria, technical_notes) must be written in SPANISH — this is the working "
    "language of the team consuming these tickets. Only the JSON field names themselves stay "
    "in English. "
    "\n\n"
    'Each ticket must be a JSON object with exactly these fields: '
    '"title" (string, short and specific — a few words naming the task, in Spanish; do not add '
    "numbering or a prefix, that is added separately), "
    '"epic" (string, in Spanish, upper case, the name of the broader feature/module this ticket '
    'belongs to, e.g. "GESTIÓN DE POOL DE TALENTO (PERSONAS)" — group related tickets from the '
    "same area of the document under the same epic name, worded consistently across tickets), "
    '"ui_route" (string, the frontend route(s)/view(s) this ticket touches, e.g. "/erp/talent/people" '
    'or "/erp/talent/people/:id" — use " | " to join more than one; empty string "" if the ticket '
    "has no UI-facing route, e.g. a pure backend/infra ticket), "
    '"backend_endpoint" (string, the backend HTTP method(s) and path this ticket touches, e.g. '
    '"GET / POST / PUT / DELETE /api/v1/talent/people" — empty string "" if the ticket has no '
    "backend endpoint, e.g. a pure frontend/static ticket), "
    '"user_story" (string, in SPANISH, following exactly this template with real line breaks: '
    '"Como <rol>,\\nquiero <objetivo>,\\npara <beneficio>." — pick whichever role best fits the '
    'document\'s context, e.g. "usuario", "administrador", "reclutador", "desarrollador"), '
    '"description" (string, in Spanish — a brief, concrete explanation of the core of the task and '
    "its flow: what needs to be built and how it should work end to end, written so anyone "
    "picking up the ticket immediately understands it without re-reading the source document; "
    "distinct from and more concrete than the user_story above), "
    '"acceptance_criteria" (array of objects, in Spanish. Each object has exactly four string '
    'fields: "name" (a short descriptive title for the criterion, e.g. "Carga y Extracción '
    'Asistida por CV"), "given" (the starting condition, phrased to follow "Dado que …"), '
    '"when" (the action or event, phrased to follow "cuando …"), and "then" (the expected '
    'outcome, phrased to follow "entonces …"). Write each part WITHOUT its leading keyword — '
    'the harness adds "Dado que", "cuando" and "entonces" when rendering. For example: '
    '{"name": "Carga de CV", "given": "el usuario está en la pantalla \'Agregar persona\'", '
    '"when": "adjunte un CV en PDF", "then": "el sistema debe extraer los datos automáticamente"}), '
    '"technical_notes" (string, in Spanish, OPTIONAL — technical details worth calling out if the '
    "document actually specifies them: limits/constraints, a permissions matrix, state/status "
    "models, integration notes, etc. Empty string \"\" if the document gives no such detail — do "
    "not invent technical notes that aren't grounded in the document. "
    "FORMATTING: anything that is literally code or machine output — SQL queries, JSON or request "
    "payloads, function or method signatures, CLI commands, error logs and stack traces, "
    "configuration snippets, permission matrices and state/status enumerations — must be wrapped "
    "in a fenced block using triple backticks on their own lines, with a language hint where it is "
    "obvious (```sql, ```json, ```bash). Everything else stays as ordinary prose; do NOT fence "
    "plain sentences, route paths or endpoint names. Example:\\n"
    "Matriz de permisos:\\n```\\ntalent:read   Lectura del directorio\\ntalent:write  "
    "Creacion y edicion\\n```), "
    '"priority" (one of: "urgent", "high", "normal", "low"), '
    '"category" (one of: "backend", "frontend", "deployment", "mundane" — the primary type of '
    'work. "backend" for a technical story: server, API, database or business logic. "frontend" '
    'for a user story delivered as UI. "deployment" for CI/CD, infra, release or ops work. '
    '"mundane" for anything general, cross-cutting or planning-oriented. This is a label for '
    "humans scanning the backlog; it does not change the ticket identifier), "
    '"parent_title" (string — use this to build a two-level hierarchy. When several tickets are '
    "pieces of one larger feature, emit one parent ticket describing that feature as a whole and "
    "set every piece's parent_title to the parent ticket's exact title; the parent must appear "
    "earlier in the array than its children. Leave it as an empty string for a top-level ticket, "
    "and never nest more than one level deep — a ticket that has a parent_title cannot itself be "
    "another ticket's parent), "
    '"sprint" (integer, 1 or higher — which 4-week Scrum sprint this ticket is planned for. '
    "Reason about this like a sprint-planning session: work through the backlog in priority "
    "and dependency order, and don't overload a single sprint — a small team can realistically "
    "finish somewhere around 8-15 right-sized tickets in one 4-week sprint, fewer if they're "
    "larger or more complex, so once a sprint is full move on to the next one. A ticket can "
    "never be scheduled in an earlier sprint than a ticket it depends on). "
    "Output ONLY the JSON array — no prose, no markdown code fences, no explanation."
)

TICKET_EXTRACTION_PROMPT = _PROMPT_HEAD + _COUNT_GUIDANCE_WHOLE + _PROMPT_TAIL


# The shared "what a ticket looks like" contract, reused verbatim by the
# idea/chat path so a ticket drafted from a one-line idea is structurally
# identical to one extracted from a full requirements document.
_TICKET_FIELDS_SPEC = _STORY_KIND_RULES + TICKET_EXTRACTION_PROMPT[
    TICKET_EXTRACTION_PROMPT.index("Each ticket must be a JSON object") :
]

IDEA_TICKET_PROMPT = (
    "You are helping someone turn a rough idea into properly-formatted work tickets. "
    "The person's idea is given below. Turn it into the smallest set of well-scoped tickets that "
    "genuinely covers it — usually just ONE ticket for a single focused idea. Only split into "
    "several tickets when the idea clearly contains distinct pieces of work (for example separate "
    "backend and frontend work, or two independent features); never produce more than 5. "
    "Do not invent scope the person did not ask for: fill in the concrete detail needed to make "
    "the ticket actionable, but stay faithful to what they actually described. "
    "\n\n"
    "All human-readable text content in the output (title, epic, user_story, description, "
    "acceptance_criteria, technical_notes) must be written in SPANISH. Only the JSON field names "
    "themselves stay in English. "
    "\n\n" + _TICKET_FIELDS_SPEC
)


def _build_idea_prompt(idea: str, history: Optional[List[dict]] = None) -> str:
    """The idea prompt, plus any earlier turns of the conversation so a
    follow-up like "split that in two" refines the previous proposal
    instead of starting over from a bare instruction."""
    if not history:
        return f"{IDEA_TICKET_PROMPT}\n\nThe idea:\n{idea}"
    transcript = "\n".join(
        f"{'Person' if turn.get('role') != 'assistant' else 'You previously proposed'}: {turn.get('content', '')}"
        for turn in history
    )
    return (
        f"{IDEA_TICKET_PROMPT}\n\nThis is a continuing conversation. Earlier turns:\n{transcript}\n\n"
        f"The person's latest instruction:\n{idea}\n\n"
        "Apply that instruction to what you proposed before and output the full corrected ticket list."
    )


def generate_tickets_from_idea(
    idea: str,
    *,
    history: Optional[List[dict]] = None,
    timeout_s: Optional[float] = None,
    max_attempts: int = 3,
    on_step: OnStep = None,
) -> Tuple[List[ProposedTicket], List[str]]:
    """Turn one free-text idea into properly-formatted proposed tickets.

    Same harnessed retry-with-repair loop as generate_tickets_from_text —
    the idea is passed inside the prompt rather than piped as a document,
    since it's a short instruction rather than a file to analyze.
    """
    if not idea or not idea.strip():
        raise TicketParseError("Describe your idea first — the message was empty.")

    claude_path = _find_claude()
    resolved_timeout = (
        timeout_s if timeout_s is not None else float(os.getenv(CLAUDE_TIMEOUT_ENV_VAR, DEFAULT_CLAUDE_TIMEOUT_S))
    )

    base_prompt = _build_idea_prompt(idea.strip(), history)
    prompt = base_prompt
    last_error: Optional[str] = None
    for attempt in range(1, max_attempts + 1):
        if last_error is not None:
            prompt = (
                f"{base_prompt}\n\nYour previous response was invalid: {last_error} "
                "Fix this and output ONLY the corrected JSON array."
            )
            _report(on_step, f"Fixing an invalid response (attempt {attempt}/{max_attempts})…")
        else:
            _report(on_step, "Drafting a ticket from your idea…")
        raw_output = _run_claude(claude_path, prompt, "", resolved_timeout)
        try:
            tickets, warnings = parse_proposed_tickets(raw_output)
        except TicketParseError as exc:
            last_error = str(exc)
            if attempt == max_attempts:
                raise
            continue
        _report(on_step, f"Drafted {len(tickets)} ticket(s).")
        return tickets, warnings
    raise AssertionError("unreachable")  # loop always returns or raises by the final attempt


def _build_extraction_prompt(
    project_start: Optional[date],
    project_end: Optional[date],
    *,
    chunk: Optional["DocumentChunk"] = None,
    chunk_total: int = 1,
) -> str:
    """The base prompt, plus a real deadline note when the caller gave one
    — without it, sprint numbers are just a relative sequence with no
    connection to an actual target date.

    When the document was split, the count guidance is swapped for this
    section's own share and the model is told where the section sits in the
    document, so it can still reason about ordering it cannot see.
    """
    if chunk is None:
        prompt = TICKET_EXTRACTION_PROMPT
    elif chunk_total <= 1:
        low, high = chunk.ticket_budget
        prompt = _PROMPT_HEAD + _count_guidance_for_document(low, high) + _PROMPT_TAIL
    else:
        low, high = chunk.ticket_budget
        prompt = _PROMPT_HEAD + _count_guidance_for_chunk(low, high) + _PROMPT_TAIL
        prompt += (
            f"\n\nIMPORTANT: you are being given SECTION {chunk.index} OF {chunk_total} of a larger "
            "requirements document, not the whole thing. Produce tickets only for the work "
            "described in this section — another pass covers the other sections, so do not "
            "restate or duplicate work that clearly belongs to a different part of the project. "
        )
        if chunk.index > 1:
            prompt += (
                "Earlier sections have already been processed, so assume the groundwork they "
                "describe exists; bias the sprint numbers you assign to later sprints accordingly. "
            )

    if not (project_start and project_end):
        return prompt
    weeks = max(1, round((project_end - project_start).days / 7))
    return (
        prompt
        + f"\n\nThe whole project must be complete by {project_end.isoformat()}, starting from "
        f"{project_start.isoformat()} — about {weeks} week(s) total, not the usual open-ended "
        "assumption. Size the sprint numbers you assign to realistically fit within that window "
        "(fewer, tighter sprints if the window is short) instead of assuming unlimited time; the "
        "harness maps your highest sprint number to the final deadline and spaces the rest evenly "
        "before it, so an unrealistically high sprint count just compresses everything further."
    )


# Generation is output-bound, and one call covering a whole large document
# both takes too long and risks being killed mid-flight. Past this much text
# the document is split into sections that are generated independently.
# ~4 chars/token, so this is roughly a 10k-token section.
CHUNK_TARGET_CHARS = 20_000
# Total tickets to aim for across the whole document, shared out per section.
TOTAL_TICKET_BUDGET = (50, 80)
# The binding constraint is OUTPUT, not input: a section asked for ~25 fully
# structured tickets came back with its JSON truncated mid-field. Each ticket
# is several hundred tokens once it carries a user story, description and
# structured criteria, so the per-section ask is capped well below the point
# where responses start getting cut off.
MAX_TICKETS_PER_CHUNK = 16
# Roughly how much source text one right-sized ticket comes from. Calibrated
# so a full-size document lands in TOTAL_TICKET_BUDGET overall, while a short
# proposal gets a handful of tickets instead of being fragmented into 80.
CHARS_PER_TICKET = 1_200


def ticket_budget_for(chars: int) -> Tuple[int, int]:
    """How many tickets to ask for from `chars` of source text.

    Asking a fixed 50-80 of every call was the bug behind both the slowness
    and the dropped responses: a 11k-character proposal was being asked for
    the same output as a 95k-character specification. The ask now scales with
    the content and is hard-capped at the volume that reliably comes back
    whole.
    """
    high = max(4, min(MAX_TICKETS_PER_CHUNK, round(chars / CHARS_PER_TICKET)))
    low = max(2, min(round(high * 0.6), high - 1))
    return low, high
# How many sections to generate at once. The wall-clock win comes from this;
# kept small so a big document doesn't spawn an unbounded number of CLI
# processes at once.
DEFAULT_CHUNK_WORKERS = 3
# Above this many tickets a chat bubble stops being readable, so the UI moves
# the batch below the conversation and says so.
CHAT_INLINE_TICKET_LIMIT = 3


@dataclass
class DocumentChunk:
    """One section of a split document, with the share of the total ticket
    budget it is responsible for."""

    index: int  # 1-based, in document order
    text: str
    ticket_budget: Tuple[int, int]


# Boundaries to break on, coarsest first. Extracted PDF text often has long
# stretches with no blank lines at all, so falling back through finer
# separators is what keeps a section anywhere near the target size.
_SPLIT_PATTERNS = (r"\n\s*\n", r"\n", r"(?<=[.!?])\s+")


def _atoms(text: str, target: int, depth: int = 0) -> List[str]:
    """Break text into pieces no larger than `target`, preferring the
    coarsest boundary that works and only hard-slicing as a last resort."""
    if len(text) <= target:
        return [text]
    if depth >= len(_SPLIT_PATTERNS):
        return [text[i : i + target] for i in range(0, len(text), target)]

    parts = [p for p in re.split(_SPLIT_PATTERNS[depth], text) if p.strip()]
    if len(parts) <= 1:
        return _atoms(text, target, depth + 1)

    pieces: List[str] = []
    for part in parts:
        pieces.extend(_atoms(part, target, depth + 1) if len(part) > target else [part])
    return pieces


def split_document(
    text: str, *, target_chars: int = CHUNK_TARGET_CHARS, budget: Tuple[int, int] = TOTAL_TICKET_BUDGET
) -> List[DocumentChunk]:
    """Split a document into sections of at most roughly `target_chars`.

    Returns a single chunk for anything that already fits, so small
    documents keep taking the original one-call path unchanged. Splits
    prefer paragraph boundaries so a requirement is not cut in half, but
    fall back to lines and then sentences — a single 60k-character block
    with no blank lines still has to be broken up, or chunking would not
    have bounded anything.
    """
    stripped = text.strip()
    if len(stripped) <= target_chars:
        # One section still gets a size-derived ask: an unsplit document is
        # not automatically a big one.
        return [DocumentChunk(index=1, text=stripped, ticket_budget=ticket_budget_for(len(stripped)))]

    sections: List[str] = []
    current: List[str] = []
    size = 0
    for paragraph in _atoms(stripped, target_chars):
        if current and size + len(paragraph) > target_chars:
            sections.append("\n\n".join(current))
            current, size = [], 0
        current.append(paragraph)
        size += len(paragraph) + 2
    if current:
        sections.append("\n\n".join(current))

    # Each section is budgeted from its own length, so a short trailing
    # section does not get the same ask as a full one.
    return [
        DocumentChunk(index=i, text=section, ticket_budget=ticket_budget_for(len(section)))
        for i, section in enumerate(sections, start=1)
    ]


class TicketExtractionError(RuntimeError):
    """Raised when document text extraction fails (bad/unsupported file, corrupt PDF)."""


class ClaudeNotFoundError(RuntimeError):
    """No usable `claude` CLI binary could be located."""


class TicketGenerationError(RuntimeError):
    """Raised when invoking the `claude` CLI fails or times out."""


class TicketParseError(RuntimeError):
    """Raised when Claude's output isn't valid JSON or contains no usable tickets."""


@dataclass
class AcceptanceCriterion:
    """One acceptance criterion, kept as its parts rather than one string.

    The trackers render criteria differently — Linear's house format puts
    the criterion's name on its own line with the Given/When/Then split
    across three bullets — which is only possible if the parts survive
    generation instead of being flattened into prose up front. `text` is
    the fallback for a model response that gave a plain string anyway.
    """

    name: str = ""
    given: str = ""
    when: str = ""
    then: str = ""
    text: str = ""

    def inline(self) -> str:
        """One-line rendering: 'Name: Dado que X, cuando Y, entonces Z.'"""
        if self.text:
            return f"{self.name}: {self.text}" if self.name else self.text
        clause = ", ".join(part for part in (self.given, self.when, self.then) if part)
        return f"{self.name}: {clause}" if self.name else clause


@dataclass
class ProposedTicket:
    """One LLM-proposed ticket, not yet created anywhere."""

    title: str
    description: str
    user_story: str = ""
    epic: str = ""
    ui_route: str = ""
    backend_endpoint: str = ""
    technical_notes: str = ""
    # Title of another ticket in the same batch that this one is a subticket
    # of; empty for a top-level ticket. Resolved to a real parent id at
    # creation time, once the parent actually exists in the tracker.
    parent_title: str = ""
    # Images and reference links carried over verbatim from an existing
    # ticket when reformatting it. Extracted in code rather than asked of
    # the model — these are long signed URLs that must survive byte-for-byte
    # or the image is lost.
    visual_resources: List[str] = field(default_factory=list)
    acceptance_criteria: List[AcceptanceCriterion] = field(default_factory=list)
    priority: str = DEFAULT_PRIORITY
    category: str = DEFAULT_CATEGORY
    sprint: int = 1
    due_date: Optional[str] = None  # ISO date, set by apply_sprint_due_dates
    assignee_clickup_id: Optional[int] = None
    assignee_linear_id: Optional[str] = None
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


def _coerce_criterion(item: object) -> Optional[AcceptanceCriterion]:
    """Accept either the structured {name, given, when, then} object the
    prompt asks for, or a bare string from a model that ignored it —
    a criterion is far too valuable to discard over its shape."""
    if isinstance(item, str):
        text = item.strip()
        if not text:
            return None
        # "Label: Dado que ..." — keep the label separate when there is one,
        # so even a legacy string still renders with a proper heading.
        name, separator, rest = text.partition(":")
        if separator and rest.strip() and len(name) <= 80 and "," not in name:
            return AcceptanceCriterion(name=name.strip(), text=rest.strip())
        return AcceptanceCriterion(text=text)

    if isinstance(item, dict):
        parts = {
            key: (item.get(key).strip() if isinstance(item.get(key), str) else "")
            for key in ("name", "given", "when", "then")
        }
        if not any(parts.values()):
            return None
        if not (parts["given"] or parts["when"] or parts["then"]):
            # Only a name came through — treat it as free text so it still shows.
            return AcceptanceCriterion(text=parts["name"])
        return AcceptanceCriterion(**parts)

    return None


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

    epic = raw.get("epic")
    if not isinstance(epic, str):
        epic = ""
    epic = epic.strip() or title.strip().upper()

    ui_route = raw.get("ui_route")
    if not isinstance(ui_route, str):
        ui_route = ""

    backend_endpoint = raw.get("backend_endpoint")
    if not isinstance(backend_endpoint, str):
        backend_endpoint = ""

    technical_notes = raw.get("technical_notes")
    if not isinstance(technical_notes, str):
        technical_notes = ""

    raw_criteria = raw.get("acceptance_criteria")
    if not isinstance(raw_criteria, list):
        warnings.append(
            f"Ticket #{index} ('{title}'): missing/invalid 'acceptance_criteria', defaulted to []"
        )
        acceptance_criteria: List[AcceptanceCriterion] = []
    else:
        acceptance_criteria = []
        dropped = 0
        for item in raw_criteria:
            criterion = _coerce_criterion(item)
            if criterion is None:
                dropped += 1
            else:
                acceptance_criteria.append(criterion)
        if dropped:
            warnings.append(f"Ticket #{index} ('{title}'): dropped {dropped} unusable acceptance_criteria entries")

    parent_title = raw.get("parent_title")
    if not isinstance(parent_title, str):
        parent_title = ""

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
        epic=epic,
        ui_route=ui_route.strip(),
        backend_endpoint=backend_endpoint.strip(),
        technical_notes=technical_notes.strip(),
        parent_title=parent_title.strip(),
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
        # Show the END of the response as well as the start, plus its length.
        # A response cut off by a dropped connection looks perfectly fine in
        # its first 500 characters — only the tail reveals it stops mid-token,
        # and quoting just the head sends you chasing the wrong cause.
        head = raw_output[:200]
        tail = raw_output[-200:] if len(raw_output) > 400 else ""
        detail = f"{len(raw_output)} chars, starts {head!r}"
        if tail:
            detail += f", ends {tail!r}"
        raise TicketParseError(f"Claude did not return valid JSON ({detail})") from exc

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
        # The CLI sometimes exits non-zero with nothing on stderr, which makes
        # a failure impossible to diagnose from the message alone — fall back
        # to whatever it managed to write to stdout.
        detail = completed.stderr.strip() or completed.stdout.strip()[:300] or "(no output)"
        raise TicketGenerationError(f"Claude CLI failed ({completed.returncode}): {detail}")

    return completed.stdout


def generate_tickets_from_text(
    document_text: str,
    *,
    timeout_s: Optional[float] = None,
    max_attempts: int = MAX_GENERATION_ATTEMPTS,
    on_step: OnStep = None,
    project_start: Optional[date] = None,
    project_end: Optional[date] = None,
    workers: Optional[int] = None,
) -> Tuple[List[ProposedTicket], List[str]]:
    """Run `claude -p <prompt>` with the document piped via stdin; parse the result.

    A document past CHUNK_TARGET_CHARS is split into sections that are
    generated concurrently and merged — one call covering a whole large
    document is output-bound to the point of hitting the timeout.

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

    chunks = split_document(document_text)
    if len(chunks) == 1:
        _report(on_step, "Sending document to Claude for ticket extraction — this can take a minute…")
        tickets, warnings = _generate_for_chunk(
            chunks[0], 1, claude_path, resolved_timeout, max_attempts,
            project_start, project_end, on_step,
        )
        _report(on_step, f"Extracted {len(tickets)} ticket(s) from the document.")
        return tickets, warnings

    return _generate_chunked(
        chunks, claude_path, resolved_timeout, max_attempts,
        project_start, project_end, on_step, workers,
    )


def _generate_for_chunk(
    chunk: DocumentChunk,
    chunk_total: int,
    claude_path: str,
    timeout_s: float,
    max_attempts: int,
    project_start: Optional[date],
    project_end: Optional[date],
    on_step: OnStep,
) -> Tuple[List[ProposedTicket], List[str]]:
    """One model call for one section, with the retry-with-repair loop.

    Retries cover two different failures. A bad *response* is repaired by
    feeding the validation error back into the next prompt. A failed
    *invocation* (the CLI exiting non-zero, which happens under load) has
    nothing to repair, so it is simply retried — previously it killed the
    whole section on the first stumble with no second chance.
    """
    base_prompt = _build_extraction_prompt(project_start, project_end, chunk=chunk, chunk_total=chunk_total)
    label = "" if chunk_total == 1 else f"Section {chunk.index}/{chunk_total}: "
    prompt = base_prompt
    last_error: Optional[str] = None
    for attempt in range(1, max_attempts + 1):
        if last_error is not None:
            _report(on_step, f"{label}{last_error} — retrying (attempt {attempt}/{max_attempts})…")
        try:
            raw_output = _run_claude(claude_path, prompt, chunk.text, timeout_s)
        except TicketGenerationError as exc:
            # Nothing to repair in the prompt — the call itself didn't run.
            last_error = str(exc)
            prompt = base_prompt
            if attempt == max_attempts:
                raise
            continue
        try:
            return parse_proposed_tickets(raw_output)
        except TicketParseError as exc:
            last_error = f"Claude's response didn't pass validation ({exc})"
            prompt = (
                f"{base_prompt}\n\nYour previous response was invalid: {exc} "
                "Fix this and output ONLY the corrected JSON array."
            )
            if attempt == max_attempts:
                raise
    raise AssertionError("unreachable")  # loop always returns or raises by the final attempt


def _generate_chunked(
    chunks: List[DocumentChunk],
    claude_path: str,
    timeout_s: float,
    max_attempts: int,
    project_start: Optional[date],
    project_end: Optional[date],
    on_step: OnStep,
    workers: Optional[int],
) -> Tuple[List[ProposedTicket], List[str]]:
    """Generate every section, several at a time, and merge the results.

    Sections run concurrently because each is an independent subprocess and
    the whole cost is waiting on output — this is where the wall-clock win
    comes from. One section failing costs only that section's tickets, the
    same per-item isolation used by the bulk QA and module sweeps; the run
    only fails outright if every section failed.
    """
    total = len(chunks)
    pool_size = max(1, min(workers or DEFAULT_CHUNK_WORKERS, total))
    _report(
        on_step,
        f"Document is large — split into {total} sections, analyzing {pool_size} at a time. "
        "Each section is a separate request, so this is far quicker than one giant call.",
    )

    lock = threading.Lock()
    done = 0

    def run(chunk: DocumentChunk):
        nonlocal done
        try:
            tickets, warnings = _generate_for_chunk(
                chunk, total, claude_path, timeout_s, max_attempts,
                project_start, project_end, on_step,
            )
            error = None
        except (TicketGenerationError, TicketParseError) as exc:
            tickets, warnings, error = [], [], str(exc)
        with lock:
            done += 1
            if error:
                _report(on_step, f"Section {chunk.index}/{total} failed ({error}) — continuing without it.")
            else:
                _report(on_step, f"Section {chunk.index}/{total} done — {len(tickets)} ticket(s). [{done}/{total}]")
        return chunk.index, tickets, warnings, error

    with ThreadPoolExecutor(max_workers=pool_size) as pool:
        results = list(pool.map(run, chunks))

    results.sort(key=lambda r: r[0])  # keep document order regardless of finish order
    merged: List[ProposedTicket] = []
    warnings: List[str] = []
    seen_titles: set = set()
    failures = 0
    for index, tickets, chunk_warnings, error in results:
        if error:
            failures += 1
            warnings.append(f"Section {index} of the document could not be analyzed: {error}")
            continue
        warnings.extend(chunk_warnings)
        for ticket in tickets:
            # Sections are cut on paragraph boundaries, so the same feature can
            # be described either side of a cut; keep the first occurrence.
            key = ticket.title.strip().lower()
            if key in seen_titles:
                continue
            seen_titles.add(key)
            merged.append(ticket)

    if failures == total:
        raise TicketParseError(
            f"All {total} sections of the document failed to produce tickets — see the warnings for details."
        )
    _report(on_step, f"Extracted {len(merged)} ticket(s) from {total - failures}/{total} sections.")
    return merged, warnings


def generate_tickets_from_file(
    filename: str,
    content: bytes,
    *,
    timeout_s: Optional[float] = None,
    on_step: OnStep = None,
    project_start: Optional[date] = None,
    project_end: Optional[date] = None,
    workers: Optional[int] = None,
) -> Tuple[List[ProposedTicket], List[str]]:
    """End-to-end: extract text from an uploaded file, then generate tickets."""
    _report(on_step, f'Extracting text from "{filename}"…')
    text = extract_document_text(filename, content)
    return generate_tickets_from_text(
        text, timeout_s=timeout_s, on_step=on_step, project_start=project_start,
        project_end=project_end, workers=workers,
    )


def apply_ticket_numbering(
    tickets: List[ProposedTicket], start_number: int = 1
) -> List[ProposedTicket]:
    """Rename tickets in order as "TAS-NN | title", counting from
    `start_number` so an existing sequence can be continued.

    One shared counter, not one per category: every ticket carries the same
    prefix now. Returns a new list — the input tickets are not mutated.
    """
    number = max(1, start_number)
    renamed: Dict[str, str] = {}
    numbered = []
    for ticket in tickets:
        new_title = f"{TICKET_PREFIX}-{number:02d} | {ticket.title}"
        number += 1
        renamed[ticket.title] = new_title
        numbered.append(replace(ticket, title=new_title))

    # parent_title points at another ticket's title, so it has to follow the
    # rename — otherwise every parent link would dangle the moment tickets
    # get their numbers.
    return [
        replace(ticket, parent_title=renamed.get(ticket.parent_title, ticket.parent_title))
        if ticket.parent_title
        else ticket
        for ticket in numbered
    ]


def apply_sprint_due_dates(
    tickets: List[ProposedTicket],
    *,
    sprint_start: Optional[date] = None,
    project_end: Optional[date] = None,
) -> List[ProposedTicket]:
    """Convert each ticket's LLM-assigned `sprint` number into a concrete
    due date.

    With no project_end, a ticket is due at the end of its assigned fixed
    4-week sprint, counted from sprint_start (today by default) — sprints
    march forward indefinitely, which is fine when there's no real
    deadline. With a project_end, the whole backlog is instead compressed
    to fit: the highest sprint number present is pinned to project_end
    exactly, and every other sprint is spaced proportionally between
    sprint_start and project_end — so the plan is always honest about a
    real target date instead of assuming unlimited time in fixed chunks.
    Returns a new list — the input tickets are not mutated.
    """
    start = sprint_start if sprint_start is not None else date.today()

    if project_end is not None and project_end > start and tickets:
        total_days = (project_end - start).days
        max_sprint = max(ticket.sprint if ticket.sprint >= 1 else 1 for ticket in tickets)
        dated = []
        for ticket in tickets:
            sprint = ticket.sprint if ticket.sprint >= 1 else 1
            offset_days = round(total_days * sprint / max_sprint)
            due = start + timedelta(days=offset_days)
            dated.append(replace(ticket, due_date=due.isoformat()))
        return dated

    dated = []
    for ticket in tickets:
        sprint = ticket.sprint if ticket.sprint >= 1 else 1
        due = start + timedelta(days=SPRINT_LENGTH_DAYS * sprint)
        dated.append(replace(ticket, due_date=due.isoformat()))
    return dated
