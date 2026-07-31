"""Exact-symbol search, to sit alongside semantic retrieval.

Embeddings are good at "where does this concept live" and weak at "where is
this exact identifier" — the very specificity of a symbol name is what the
embedding blurs away. Lexical search covers that, and the two together beat
either alone.

ripgrep is used when present because it is fast, but it is not assumed: it is
not installed by default on most systems and cannot be bundled in the release.
`git grep` is the fallback, and git is already a dependency of indexing, so
there is always a working path.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

from meta_harness.embeddings.chunking import language_for

# A symbol search that returns thousands of lines is a failed search; the cap
# keeps a common identifier like `id` from flooding the fusion step.
DEFAULT_LIMIT = 40
SEARCH_TIMEOUT_S = 30.0

BACKEND_RIPGREP = "ripgrep"
BACKEND_GIT_GREP = "git-grep"
BACKEND_NONE = "none"


@dataclass(frozen=True)
class LexicalHit:
    """One line matching a symbol, with the location needed to cite it."""

    path: str
    line: int
    text: str

    @property
    def location(self) -> str:
        return f"{self.path}:{self.line}"


def ripgrep_path() -> Optional[str]:
    """The ripgrep binary, or None.

    Deliberately resolves via PATH rather than trusting a shell alias — an
    interactive shell may define `rg` as a function, which a subprocess will
    never see.
    """
    return shutil.which("rg")


def available_backend() -> str:
    """Which lexical backend will actually be used."""
    if ripgrep_path():
        return BACKEND_RIPGREP
    if shutil.which("git"):
        return BACKEND_GIT_GREP
    return BACKEND_NONE


def _run(command: Sequence[str], cwd: Path) -> Optional[str]:
    """Run a search command; None when it could not run at all.

    Both tools exit 1 to mean "no matches", which is an ordinary outcome
    rather than a failure — only a higher code is a real error.
    """
    try:
        completed = subprocess.run(
            list(command), cwd=str(cwd), capture_output=True, text=True, timeout=SEARCH_TIMEOUT_S
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode not in (0, 1):
        return None
    return completed.stdout


def _ripgrep(symbol: str, root: Path, limit: int, whole_word: bool) -> Optional[List[LexicalHit]]:
    binary = ripgrep_path()
    if not binary:
        return None

    command = [binary, "--json", "--line-number", "--no-heading", "--max-count", str(limit)]
    if whole_word:
        command.append("--word-regexp")
    command += ["--fixed-strings", symbol, "."]

    output = _run(command, root)
    if output is None:
        return None

    hits: List[LexicalHit] = []
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "match":
            continue
        data = event.get("data") or {}
        path = ((data.get("path") or {}).get("text") or "").lstrip("./")
        text = ((data.get("lines") or {}).get("text") or "").rstrip("\n")
        number = data.get("line_number")
        if path and number:
            hits.append(LexicalHit(path=path, line=int(number), text=text))
    return hits


def _git_grep(symbol: str, root: Path, limit: int, whole_word: bool) -> Optional[List[LexicalHit]]:
    """Fallback search. Covers tracked and untracked-but-not-ignored files,
    matching what the indexer considers part of the repository."""
    command = ["git", "grep", "-n", "-I", "--fixed-strings", "--untracked"]
    if whole_word:
        command.append("-w")
    command += ["-e", symbol]

    output = _run(command, root)
    if output is None:
        return None

    hits: List[LexicalHit] = []
    for line in output.splitlines():
        # path:line:text — the text itself may contain colons, so split twice.
        parts = line.split(":", 2)
        if len(parts) != 3 or not parts[1].isdigit():
            continue
        hits.append(LexicalHit(path=parts[0], line=int(parts[1]), text=parts[2]))
        if len(hits) >= limit:
            break
    return hits


def find_symbol(
    symbol: str,
    *,
    root: Path,
    limit: int = DEFAULT_LIMIT,
    whole_word: bool = True,
    indexable_only: bool = True,
) -> List[LexicalHit]:
    """Find lines containing `symbol` under `root`.

    Matches are literal, not regex: callers pass identifiers, and a name like
    `Foo.bar(` would otherwise be interpreted as a pattern. `whole_word` keeps
    a search for `id` from matching `identifier`.
    """
    symbol = symbol.strip()
    if not symbol:
        raise ValueError("symbol is required")

    root = Path(root).expanduser().resolve()
    hits = _ripgrep(symbol, root, limit, whole_word)
    if hits is None:
        hits = _git_grep(symbol, root, limit, whole_word)
    if hits is None:
        return []

    if indexable_only:
        # Keep lexical and semantic results drawn from the same universe of
        # files, or fusion compares hits the index could never have produced.
        hits = [hit for hit in hits if language_for(Path(hit.path)) is not None]
    return hits[:limit]


# A symbol is an identifier: something you could grep for and expect to find a
# declaration. A natural-language question is not, and searching for one
# literally returns nothing useful.
_SYMBOL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")


def looks_like_symbol(query: str) -> bool:
    """Whether a query is worth running through lexical search.

    Used to decide automatically: "calculateInvoiceTotal" yes, "how are
    tickets created" no.
    """
    candidate = query.strip()
    if not candidate or " " in candidate:
        return False
    return bool(_SYMBOL_RE.match(candidate))
