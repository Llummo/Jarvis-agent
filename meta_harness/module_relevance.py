"""Decide whether a ticket actually belongs to a given product module.

The real question this answers is the one a senior asks in review: "check
whether this ticket relates to the People module". Answering it well needs
two things the harness already knows how to get — the ticket's real text
from the tracker, and a description of the module — plus a judgement call,
which is what the Claude CLI is for.

The module description can be pasted documentation, or it can be retrieved
from an embedding index of the actual repository. The second is why this
module no longer requires a human to paste anything.

Same subprocess boundary and retry-with-repair harness as qa_flow.py.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from meta_harness.clickup_bridge import ClickUpReadError, get_clickup_task
from meta_harness.embeddings import GeminiEmbedder, VectorStore, VectorStoreError, search_repository
from meta_harness.embeddings.embedder import TASK_CODE_QUERY
from meta_harness.linear_bridge import LinearReadError, get_linear_issue

OnStep = Optional[Callable[[str], None]]


def _report(on_step: OnStep, message: str) -> None:
    if on_step is not None:
        on_step(message)


class ModuleContextUnavailableError(RuntimeError):
    """Raised when a module's context cannot be retrieved from the index."""


TRACKERS = ("clickup", "linear")
VERDICTS = ("related", "partially_related", "unrelated")

CLAUDE_PATH_ENV_VAR = "META_HARNESS_CLAUDE_PATH"
CLAUDE_TIMEOUT_ENV_VAR = "META_HARNESS_MODULE_CHECK_TIMEOUT_S"
DEFAULT_TIMEOUT_S = 180.0
MAX_ATTEMPTS = 3

# How many retrieved spans describe a module when the context is not pasted.
# Enough to cover a module's surface; small enough that the judgement stays
# about the module rather than the whole repository.
DEFAULT_CONTEXT_CHUNKS = 12

PROMPT_TEMPLATE = (
    "You are reviewing whether a work ticket belongs to a specific product module. "
    "Below you are given a description of the module — either its written documentation "
    "or source code retrieved from the repository, each span labelled with its file and "
    "line range — and then the ticket. "
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


def fetch_ticket(ticket_id: str, tracker: str) -> Tuple[str, str]:
    """Normalize a ticket from either tracker to (name, description)."""
    if tracker not in TRACKERS:
        raise ValueError(f"Invalid tracker '{tracker}'; must be one of {TRACKERS}")
    if tracker == "linear":
        issue = get_linear_issue(ticket_id)
        return issue.get("title") or ticket_id, issue.get("description") or ""
    task = get_clickup_task(ticket_id)
    return task.get("name") or ticket_id, task.get("text_content") or task.get("description") or ""


def retrieve_module_context(
    module_name: str,
    *,
    repo: str,
    limit: int = DEFAULT_CONTEXT_CHUNKS,
    on_step: OnStep = None,
) -> str:
    """Assemble a module description by retrieving source from the index.

    Each retrieved span keeps its file path and line range: the judgement that
    follows is expected to cite evidence, and a citation to `people/service.go:88`
    is checkable in a way that an unattributed snippet is not.
    """
    _report(on_step, f'Retrieving "{module_name}" source from the "{repo}" index…')
    store = VectorStore()
    try:
        hits = search_repository(
            module_name,
            repo=repo,
            embedder=GeminiEmbedder(query_task_type=TASK_CODE_QUERY),
            store=store,
            limit=limit,
        )
    except VectorStoreError as exc:
        # A missing or mis-shaped index is a "no context available" outcome
        # for this caller, not a storage-layer detail to leak upward.
        raise ModuleContextUnavailableError(str(exc)) from exc
    finally:
        store.close()

    if not hits:
        raise ModuleContextUnavailableError(
            f'Nothing in the "{repo}" index matched "{module_name}". '
            "Check the module name, or rebuild the index."
        )

    _report(
        on_step,
        f"Retrieved {len(hits)} span(s): " + ", ".join(hit.location for hit in hits[:5]),
    )
    sections = [
        f"--- {hit.location} (relevance {hit.score:.2f})\n{hit.text}" for hit in hits
    ]
    return (
        f"Source retrieved from the '{repo}' repository for the "
        f"'{module_name}' module:\n\n" + "\n\n".join(sections)
    )


def analyze_module_relevance(
    ticket_id: str,
    *,
    tracker: str = "clickup",
    module_name: str,
    module_context: Optional[str] = None,
    repo: Optional[str] = None,
    context_limit: int = DEFAULT_CONTEXT_CHUNKS,
    timeout_s: Optional[float] = None,
    max_attempts: int = MAX_ATTEMPTS,
    on_step: OnStep = None,
) -> ModuleRelevance:
    """Fetch the ticket from its tracker and judge it against the module.

    The module can be described two ways. Pass `module_context` to supply the
    documentation directly, or pass `repo` to retrieve the relevant source
    from an embedding index instead — the second is what removes the manual
    paste that used to be mandatory here. Supplying both prefers the explicit
    text, on the principle that a human who typed something meant it.

    Read-only: this never writes to either tracker, so it's safe to run
    against anything, including work that is already done.
    """
    if not module_name.strip():
        raise ValueError("module_name is required")

    module_context = (module_context or "").strip()
    if not module_context:
        if not repo:
            raise ValueError(
                "Describe the module either by passing module_context (its documentation) "
                "or repo (an indexed repository to retrieve it from). "
                "Index one with: meta-harness index build --repo <path>"
            )
        module_context = retrieve_module_context(
            module_name, repo=repo, limit=context_limit, on_step=on_step
        )

    _report(on_step, f"Fetching ticket {ticket_id} from {'Linear' if tracker == 'linear' else 'ClickUp'}…")
    ticket_name, ticket_description = fetch_ticket(ticket_id, tracker)
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


# The order a reviewer actually wants to read results in: what belongs to the
# module first, then the borderline cases, then everything ruled out.
_VERDICT_ORDER = {"related": 0, "partially_related": 1, "unrelated": 2}

VERDICT_LABELS = {
    "related": "Pertenece al módulo",
    "partially_related": "Parcialmente relacionado",
    "unrelated": "No pertenece al módulo",
}


def analyze_modules_bulk(
    ticket_ids: Sequence[str],
    *,
    tracker: str = "clickup",
    module_name: str,
    module_context: Optional[str] = None,
    repo: Optional[str] = None,
    context_limit: int = DEFAULT_CONTEXT_CHUNKS,
    timeout_s: Optional[float] = None,
    on_step: OnStep = None,
) -> List[Tuple[str, Optional[ModuleRelevance], Optional[str]]]:
    """Check many tickets against one module in a single sweep.

    Read-only, and one ticket failing (a bad id, a Claude hiccup) never
    stops the rest — same per-item isolation as the bulk QA sweep. Returns
    (ticket_id, relevance-or-None, error-or-None) in the same order as
    ticket_ids; sorting for presentation is the caller's job.
    """
    results: List[Tuple[str, Optional[ModuleRelevance], Optional[str]]] = []
    # Resolve the module description once, not once per ticket. Retrieval is
    # keyed on the module, so doing it inside the loop would issue N identical
    # queries and — worse — let the description drift between tickets in the
    # same sweep, making their verdicts incomparable.
    resolved_context = (module_context or "").strip()
    if not resolved_context and repo:
        resolved_context = retrieve_module_context(
            module_name, repo=repo, limit=context_limit, on_step=on_step
        )

    total = len(ticket_ids)
    for index, ticket_id in enumerate(ticket_ids, start=1):
        _report(on_step, f"Checking {index}/{total} against “{module_name}”…")
        try:
            relevance = analyze_module_relevance(
                ticket_id,
                tracker=tracker,
                module_name=module_name,
                module_context=resolved_context,
                timeout_s=timeout_s,
            )
            results.append((ticket_id, relevance, None))
        except (ClickUpReadError, LinearReadError, ModuleRelevanceError) as exc:
            results.append((ticket_id, None, str(exc)))

    aligned = sum(1 for _tid, rel, err in results if rel and not err and rel.is_related)
    _report(on_step, f"Done — {aligned} of {total} ticket(s) align with “{module_name}”.")
    return results


def sort_by_relevance(
    results: Sequence[Tuple[str, Optional[ModuleRelevance], Optional[str]]],
) -> List[Tuple[str, Optional[ModuleRelevance], Optional[str]]]:
    """Related first, then partially related, then unrelated, then errors —
    with the most confident calls at the top of each group."""

    def key(item):
        _ticket_id, relevance, error = item
        if error or relevance is None:
            return (3, 0.0)
        return (_VERDICT_ORDER.get(relevance.verdict, 2), -relevance.confidence)

    return sorted(results, key=key)


def render_module_report_markdown(
    module_name: str,
    results: Sequence[Tuple[str, Optional[ModuleRelevance], Optional[str]]],
) -> str:
    """Render a whole sweep as one shareable Markdown report, leading with
    the list of tickets that actually belong to the module — that list is
    the answer to the question being asked."""
    ordered = sort_by_relevance(results)
    analyzed = [(tid, rel) for tid, rel, err in ordered if rel and not err]
    failed = [(tid, err) for tid, rel, err in ordered if err or rel is None]
    by_verdict = {
        verdict: [(tid, rel) for tid, rel in analyzed if rel.verdict == verdict] for verdict in VERDICTS
    }

    lines = [
        f"# Módulo: {module_name}",
        "",
        f"- **Generado:** {datetime.now(timezone.utc).isoformat()}",
        f"- **Tickets analizados:** {len(analyzed)} de {len(results)}",
        f"- **Pertenecen al módulo:** {len(by_verdict['related'])}",
        f"- **Parcialmente relacionados:** {len(by_verdict['partially_related'])}",
        f"- **No pertenecen:** {len(by_verdict['unrelated'])}",
    ]
    if failed:
        lines.append(f"- **No se pudieron analizar:** {len(failed)}")

    aligned = by_verdict["related"] + by_verdict["partially_related"]
    lines += ["", "## Tickets que se alinean con el módulo", ""]
    if aligned:
        for _ticket_id, relevance in aligned:
            marker = "✅" if relevance.verdict == "related" else "🟡"
            lines.append(
                f"- {marker} **{relevance.ticket_name}** — {VERDICT_LABELS[relevance.verdict]} "
                f"({relevance.confidence:.0%})"
            )
    else:
        lines.append("_Ningún ticket analizado pertenece a este módulo._")

    lines += ["", "## Detalle", ""]
    for _ticket_id, relevance in analyzed:
        lines += [
            f"### {relevance.ticket_name} — {VERDICT_LABELS[relevance.verdict]} ({relevance.confidence:.0%})",
            "",
            f"- **Ticket ID:** {relevance.ticket_id}",
            "",
            relevance.rationale,
        ]
        if relevance.matched_aspects:
            lines += ["", "**Coincide en:**", ""]
            lines += [f"- {aspect}" for aspect in relevance.matched_aspects]
        if relevance.module_gaps:
            lines += ["", "**Fuera del módulo:**", ""]
            lines += [f"- {gap}" for gap in relevance.module_gaps]
        lines.append("")

    if failed:
        lines += ["## No se pudieron analizar", ""]
        lines += [f"- `{ticket_id}`: {error}" for ticket_id, error in failed]
        lines.append("")

    return "\n".join(lines) + "\n"
