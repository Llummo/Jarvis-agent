"""Resolve a pasted list of ticket names to real Linear issues.

The names arrive the way a human sends them — copied out of a chat, wrapped
in bullets and bold markers, with a relevance verdict stuck on the end:

    - ✅ *6.5 Tabla de Carga por responsable* — Pertenece al módulo (93%) *check*

So the first job is stripping the decoration back to a title, and the second is
finding which issue that title means. Neither is exact: the pasted name is often
an abbreviation of the real one, or carries a typo, or differs by an accent.

Matching therefore runs in three passes, cheapest first:

1. An explicit identifier (`SIG-60`) in the line — unambiguous, so it wins.
2. Scoring on a normalized title, with the numbered prefix (`6.5`, `1.16`)
   weighted heavily. Those prefixes are the team's own taxonomy and are far
   more discriminating than any word in the title.
3. A model, but only for names where scoring found no clear winner.

ripgrep narrows the corpus before scoring. It is an accelerator and never the
source of truth: if it returns nothing the full scan still runs, so a missing
binary or an odd character class can cost time but not a match.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

# Accept outright: the title is effectively the same string.
STRONG_MATCH = 0.82
# Below this a candidate is not worth showing at all.
MIN_CANDIDATE = 0.35
# Two candidates this close cannot be separated by scoring alone.
AMBIGUITY_MARGIN = 0.08
# How many candidates survive to the reasoning step or the UI.
MAX_CANDIDATES = 5

STATUS_MATCHED = "matched"
STATUS_AMBIGUOUS = "ambiguous"
STATUS_UNMATCHED = "unmatched"


@dataclass(frozen=True)
class Thresholds:
    """Where the matcher draws its three lines.

    Grouped and injectable so they can be varied and measured rather than
    argued about: the defaults are a starting guess, and `rework.benchmark`
    exists to find out whether they are the right one.
    """

    strong_match: float = STRONG_MATCH
    min_candidate: float = MIN_CANDIDATE
    ambiguity_margin: float = AMBIGUITY_MARGIN
    max_candidates: int = MAX_CANDIDATES

    def validate(self) -> None:
        if not 0 < self.strong_match <= 1:
            raise ValueError("strong_match must be within (0, 1]")
        if not 0 <= self.min_candidate < self.strong_match:
            raise ValueError("min_candidate must be below strong_match")
        if self.ambiguity_margin < 0:
            raise ValueError("ambiguity_margin cannot be negative")
        if self.max_candidates < 1:
            raise ValueError("max_candidates must be at least 1")

    def describe(self) -> Dict[str, float]:
        return {
            "strong_match": self.strong_match,
            "min_candidate": self.min_candidate,
            "ambiguity_margin": self.ambiguity_margin,
            "max_candidates": self.max_candidates,
        }


DEFAULT_THRESHOLDS = Thresholds()

GREP_TIMEOUT_S = 15.0

# A Linear identifier: two-to-six capitals, a dash, digits.
_IDENTIFIER_RE = re.compile(r"\b([A-Z]{2,6}-\d+)\b")
# A leading section number: "6.5", "1.16.", "4."
_NUMERIC_PREFIX_RE = re.compile(r"^(\d+(?:\.\d+)*)\.?(?=\s|$)")
# The verdict a relevance report appends after an em dash.
_VERDICT_TAIL_RE = re.compile(
    r"\s*[—–-]\s*(?:pertenece\b|parcialmente\b|no\s+pertenece\b|relacionado\b|unrelated\b).*$",
    re.IGNORECASE,
)
_TRAILING_NOISE_RE = re.compile(r"\s*\(\s*\d{1,3}\s*%\s*\)\s*|\bcheck\b", re.IGNORECASE)
_LEADING_BULLET_RE = re.compile(r"^\s*(?:[-*•]\s*)+")
_STATUS_EMOJI = "✅❌⚠️☑️✔️🔲🔳"


def strip_accents(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize(value: str) -> str:
    """Fold a title to the form two spellings of it will agree on."""
    folded = strip_accents(value).lower()
    folded = re.sub(r"[^a-z0-9\s]+", " ", folded)
    return re.sub(r"\s+", " ", folded).strip()


def tokens(value: str) -> List[str]:
    # One- and two-letter words are almost all Spanish articles and
    # prepositions; keeping them makes every title look alike.
    return [token for token in normalize(value).split() if len(token) > 2]


@dataclass(frozen=True)
class ParsedName:
    """One line of pasted input, reduced to what can be matched on."""

    raw: str
    title: str
    numeric_prefix: str = ""
    identifier: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.title.strip()


def parse_line(line: str) -> Optional[ParsedName]:
    """Reduce one pasted line to a title, or None if it carries no name."""
    text = line.strip()
    if not text:
        return None

    text = _LEADING_BULLET_RE.sub("", text)
    text = text.lstrip(_STATUS_EMOJI + " ")
    text = _LEADING_BULLET_RE.sub("", text)

    identifier_match = _IDENTIFIER_RE.search(text)
    identifier = identifier_match.group(1) if identifier_match else ""

    # Drop the verdict a relevance report appends, but only when the tail
    # actually looks like one — an em dash inside a real title must survive.
    text = _VERDICT_TAIL_RE.sub("", text)
    text = _TRAILING_NOISE_RE.sub(" ", text)
    # Bold/italic markers are decoration in every chat client that uses them.
    text = text.replace("*", " ").replace("_", " ")
    text = re.sub(r"\s+", " ", text).strip(" .-–—")

    if identifier:
        # Keep the human-readable part; the identifier already matched exactly.
        text = _IDENTIFIER_RE.sub("", text)
        text = text.replace("[]", " ").strip(" []-–—")
        text = re.sub(r"\s+", " ", text).strip()

    prefix_match = _NUMERIC_PREFIX_RE.match(text)
    numeric_prefix = prefix_match.group(1) if prefix_match else ""

    if not text and not identifier:
        return None
    return ParsedName(
        raw=line.strip(), title=text, numeric_prefix=numeric_prefix, identifier=identifier
    )


def parse_pasted_names(text: str) -> List[ParsedName]:
    """Parse a pasted block into names, skipping blank and decorative lines."""
    parsed = []
    for line in text.splitlines():
        name = parse_line(line)
        if name is not None and not name.is_empty:
            parsed.append(name)
    return parsed


def _numeric_prefix_of(title: str) -> str:
    match = _NUMERIC_PREFIX_RE.match(title.strip())
    return match.group(1) if match else ""


def score_title(parsed: ParsedName, issue_title: str) -> float:
    """How well a pasted name matches a real title, in 0..1.

    Token containment rather than symmetric overlap: a pasted name is
    routinely a shortened form of the real title, and penalising the real
    title for carrying extra words would rank the correct issue below a
    terser wrong one.
    """
    left, right = normalize(parsed.title), normalize(issue_title)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0

    left_tokens, right_tokens = set(tokens(parsed.title)), set(tokens(issue_title))
    if not left_tokens or not right_tokens:
        # Titles made entirely of short words — fall back to raw containment.
        return 0.6 if left in right or right in left else 0.0

    shared = left_tokens & right_tokens
    containment = len(shared) / min(len(left_tokens), len(right_tokens))
    coverage = len(shared) / max(len(left_tokens), len(right_tokens))
    score = 0.7 * containment + 0.3 * coverage

    # The team's own numbering is the strongest signal available: two tickets
    # sharing "6.5" are near-certainly the same item, and two that disagree on
    # it are near-certainly not.
    issue_prefix = _numeric_prefix_of(issue_title)
    if parsed.numeric_prefix and issue_prefix:
        if parsed.numeric_prefix == issue_prefix:
            score = min(1.0, score + 0.35)
        else:
            score *= 0.45
    return round(min(score, 1.0), 4)


@dataclass(frozen=True)
class IssueMatch:
    """One candidate issue for a pasted name."""

    issue_id: str
    identifier: str
    title: str
    score: float
    state_name: str = ""
    state_type: str = ""

    @classmethod
    def from_issue(cls, issue: Dict, score: float) -> "IssueMatch":
        state = issue.get("state") or {}
        return cls(
            issue_id=str(issue.get("id") or ""),
            identifier=str(issue.get("identifier") or ""),
            title=str(issue.get("title") or ""),
            score=score,
            state_name=str(state.get("name") or ""),
            state_type=str(state.get("type") or ""),
        )


@dataclass
class NameResolution:
    """What one pasted name resolved to."""

    parsed: ParsedName
    status: str
    candidates: List[IssueMatch] = field(default_factory=list)
    chosen: Optional[IssueMatch] = None
    method: str = ""
    note: str = ""

    @property
    def name(self) -> str:
        return self.parsed.title or self.parsed.identifier


# A reasoner picks one candidate for an ambiguous name, or None to leave it
# unresolved. Injected so the resolution logic can be tested without a model,
# and so a caller can decline reasoning entirely.
Reasoner = Callable[[ParsedName, Sequence[IssueMatch]], Optional[str]]


def _ripgrep() -> Optional[str]:
    return shutil.which("rg")


def _corpus_lines(issues: Sequence[Dict]) -> List[str]:
    return [
        f"{issue.get('id', '')}\t{normalize(str(issue.get('title') or ''))}" for issue in issues
    ]


def grep_narrow(
    parsed: ParsedName, issues: Sequence[Dict], corpus_path: Optional[Path]
) -> Optional[List[Dict]]:
    """Issues whose title contains the name's most distinctive token.

    Returns None when narrowing could not run or found nothing, which the
    caller reads as "score everything" — grep speeds the common case up and is
    never allowed to decide a match on its own.
    """
    binary = _ripgrep()
    if binary is None or corpus_path is None or not corpus_path.exists():
        return None

    needle = parsed.numeric_prefix or max(tokens(parsed.title), key=len, default="")
    if len(needle) < 3:
        return None

    try:
        completed = subprocess.run(
            [binary, "--fixed-strings", "--ignore-case", "--no-line-number", needle, str(corpus_path)],
            capture_output=True,
            text=True,
            timeout=GREP_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode not in (0, 1):
        return None

    hit_ids = {line.split("\t", 1)[0] for line in completed.stdout.splitlines() if "\t" in line}
    if not hit_ids:
        return None
    return [issue for issue in issues if str(issue.get("id") or "") in hit_ids]


def _rank(parsed: ParsedName, issues: Sequence[Dict], thresholds: Thresholds) -> List[IssueMatch]:
    scored = [
        IssueMatch.from_issue(issue, score_title(parsed, str(issue.get("title") or "")))
        for issue in issues
    ]
    scored = [match for match in scored if match.score >= thresholds.min_candidate]
    scored.sort(key=lambda match: (-match.score, match.identifier))
    return scored[: thresholds.max_candidates]


def resolve_names(
    names: Sequence[ParsedName],
    issues: Sequence[Dict],
    *,
    reasoner: Optional[Reasoner] = None,
    thresholds: Thresholds = DEFAULT_THRESHOLDS,
) -> List[NameResolution]:
    """Resolve every pasted name against the team's issues."""
    thresholds.validate()
    by_identifier = {
        str(issue.get("identifier") or "").upper(): issue for issue in issues if issue.get("identifier")
    }

    corpus_path: Optional[Path] = None
    handle = None
    if _ripgrep() is not None and issues:
        handle = tempfile.NamedTemporaryFile("w", suffix=".tsv", delete=False, encoding="utf-8")
        handle.write("\n".join(_corpus_lines(issues)))
        handle.close()
        corpus_path = Path(handle.name)

    try:
        return [
            _resolve_one(name, issues, by_identifier, corpus_path, reasoner, thresholds)
            for name in names
        ]
    finally:
        if corpus_path is not None:
            corpus_path.unlink(missing_ok=True)


def _resolve_one(
    parsed: ParsedName,
    issues: Sequence[Dict],
    by_identifier: Dict[str, Dict],
    corpus_path: Optional[Path],
    reasoner: Optional[Reasoner],
    thresholds: Thresholds = DEFAULT_THRESHOLDS,
) -> NameResolution:
    # An identifier is exact; nothing downstream can improve on it.
    if parsed.identifier:
        issue = by_identifier.get(parsed.identifier.upper())
        if issue is not None:
            return NameResolution(
                parsed=parsed,
                status=STATUS_MATCHED,
                candidates=[IssueMatch.from_issue(issue, 1.0)],
                chosen=IssueMatch.from_issue(issue, 1.0),
                method="identifier",
            )

    narrowed = grep_narrow(parsed, issues, corpus_path)
    candidates = _rank(parsed, narrowed if narrowed is not None else issues, thresholds)
    if narrowed is not None and not candidates:
        # Narrowing threw away the answer; pay for the full scan rather than
        # report a name as missing because of a grep miss.
        candidates = _rank(parsed, issues, thresholds)

    if not candidates:
        return NameResolution(parsed=parsed, status=STATUS_UNMATCHED, note="no issue scored high enough")

    best = candidates[0]
    runner_up = candidates[1] if len(candidates) > 1 else None
    separated = runner_up is None or (best.score - runner_up.score) > thresholds.ambiguity_margin

    if best.score >= thresholds.strong_match and separated:
        return NameResolution(
            parsed=parsed, status=STATUS_MATCHED, candidates=candidates, chosen=best, method="score"
        )

    if reasoner is not None:
        chosen_id = reasoner(parsed, candidates)
        if chosen_id:
            for candidate in candidates:
                if candidate.issue_id == chosen_id:
                    return NameResolution(
                        parsed=parsed,
                        status=STATUS_MATCHED,
                        candidates=candidates,
                        chosen=candidate,
                        method="reasoning",
                    )

    return NameResolution(
        parsed=parsed,
        status=STATUS_AMBIGUOUS,
        candidates=candidates,
        note="scoring could not separate the top candidates",
    )
