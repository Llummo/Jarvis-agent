"""Split source files into retrievable chunks.

Fixed-size windows are the wrong tool for code: they cut functions in half,
so neither half retrieves well and the half without the signature is close to
meaningless. This splits at declaration boundaries instead, then packs
consecutive declarations together until the model's input budget is reached.

Every chunk is prefixed with its file path before embedding. A function body
on its own is ambiguous — `func Create(...)` could belong to any module — and
the path is the cheapest possible context that disambiguates it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from meta_harness.embeddings.embedder import MAX_INPUT_TOKENS

# Code tokenizes less efficiently than prose — punctuation and identifiers
# fragment more — so budget characters at a pessimistic ratio rather than the
# ~4 chars/token that holds for English.
CHARS_PER_TOKEN = 3.2

# Leave room for the path header and any tokenizer disagreement.
BUDGET_SAFETY = 0.85

MAX_CHUNK_CHARS = int(MAX_INPUT_TOKENS * CHARS_PER_TOKEN * BUDGET_SAFETY)

# Below this, a "chunk" is usually an import block or a closing brace — noise
# that dilutes the index without ever being a useful search result.
MIN_CHUNK_CHARS = 60

# Lines that begin a new top-level unit, per language. Matched against the
# line with leading whitespace stripped, so nested declarations (methods in a
# TypeScript class) split too, which is what you want for retrieval.
_BOUNDARY_PATTERNS: Dict[str, str] = {
    "go": r"^(func|type|var|const|package)\s",
    "typescript": r"^(export|function|async\s+function|class|interface|type|const|let|enum)\s",
    "python": r"^(def|class|async\s+def)\s",
    "sql": r"^(CREATE|ALTER|DROP|INSERT|UPDATE|DELETE|WITH|SELECT|BEGIN|COMMENT)\s",
    "java": r"^(public|private|protected|class|interface|enum|record)\s",
    "rust": r"^(fn|pub\s+fn|struct|enum|impl|trait|mod)\s",
}

# Extension -> language. Also the allowlist: anything not here is not indexed,
# which is what keeps images, lockfiles and build output out of the index.
LANGUAGE_BY_SUFFIX: Dict[str, str] = {
    ".go": "go",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "typescript",
    ".jsx": "typescript",
    ".mjs": "typescript",
    ".py": "python",
    ".sql": "sql",
    ".java": "java",
    ".kt": "java",
    ".rs": "rust",
    ".md": "markdown",
    ".mdx": "markdown",
    ".txt": "text",
    ".rst": "text",
    ".yaml": "text",
    ".yml": "text",
    ".toml": "text",
    ".json": "text",
}


@dataclass(frozen=True)
class Chunk:
    """One indexable span of a file, with the location needed to cite it."""

    path: str
    language: str
    start_line: int
    end_line: int
    text: str

    def embed_text(self) -> str:
        """The text actually sent to the embedding model.

        The path header is what lets a natural-language query like "user
        permissions" reach a file named `authz/rbac.go` whose body never says
        "permission" in those words.
        """
        return f"# file: {self.path} (lines {self.start_line}-{self.end_line})\n{self.text}"


def language_for(path: Path) -> Optional[str]:
    """The language to chunk this file as, or None if it isn't indexable."""
    return LANGUAGE_BY_SUFFIX.get(path.suffix.lower())


def _split_points(lines: Sequence[str], language: str) -> List[int]:
    """Line indices where a new unit starts.

    Markdown splits on headings; code splits on declarations; anything else
    has no meaningful boundary, so it packs by size alone.
    """
    if language == "markdown":
        pattern = re.compile(r"^#{1,6}\s")
    else:
        raw = _BOUNDARY_PATTERNS.get(language)
        if raw is None:
            return [0]
        pattern = re.compile(raw)

    points = [
        index for index, line in enumerate(lines) if pattern.match(line.strip())
    ]
    # Always start at the top so a file's leading imports/licence aren't
    # silently dropped when the first declaration appears on line 40.
    if not points or points[0] != 0:
        points.insert(0, 0)
    return points


def _split_long_lines(lines: Sequence[str]) -> List[str]:
    """Break individual lines that exceed the budget on their own.

    Line-based splitting has a floor: a minified bundle, a generated SQL seed,
    or a single-line JSON fixture can be one line of hundreds of kilobytes.
    Without this the chunk would be emitted over budget and silently truncated
    by the backend, losing the tail with no error.
    """
    result: List[str] = []
    for line in lines:
        if len(line) <= MAX_CHUNK_CHARS:
            result.append(line)
            continue
        for start in range(0, len(line), MAX_CHUNK_CHARS):
            result.append(line[start : start + MAX_CHUNK_CHARS])
    return result


def _hard_split(text: str, start_line: int, path: str, language: str) -> List[Chunk]:
    """Break a single oversized unit into budget-sized pieces.

    A generated file or a 2,000-line function has no internal boundary to use,
    so this falls back to splitting on line count. Pieces overlap by one line
    so a declaration sitting exactly on a seam still appears whole in one of
    them.
    """
    lines = _split_long_lines(text.splitlines())
    chunks: List[Chunk] = []
    current: List[str] = []
    current_start = start_line

    for offset, line in enumerate(lines):
        projected = sum(len(item) + 1 for item in current) + len(line)
        if current and projected > MAX_CHUNK_CHARS:
            body = "\n".join(current)
            chunks.append(
                Chunk(
                    path=path,
                    language=language,
                    start_line=current_start,
                    end_line=current_start + len(current) - 1,
                    text=body,
                )
            )
            # Carry one line across the seam so a declaration sitting exactly
            # on the boundary still appears whole somewhere — but only when
            # that line is small. After `_split_long_lines`, a "line" can be a
            # full budget-sized slice, and prepending one of those would
            # produce a chunk twice the budget.
            tail = current[-1]
            if len(tail) <= MAX_CHUNK_CHARS // 5:
                current = [tail, line]
                current_start = start_line + offset - 1
            else:
                current = [line]
                current_start = start_line + offset
        else:
            current.append(line)

    if current:
        body = "\n".join(current)
        if body.strip():
            chunks.append(
                Chunk(
                    path=path,
                    language=language,
                    start_line=current_start,
                    end_line=current_start + len(current) - 1,
                    text=body,
                )
            )
    return chunks


def chunk_text(text: str, *, path: str, language: str) -> List[Chunk]:
    """Split one file's contents into chunks, in file order."""
    if not text.strip():
        return []

    lines = text.splitlines()
    points = _split_points(lines, language)
    # Sentinel so the final unit has an end.
    bounds = points + [len(lines)]

    chunks: List[Chunk] = []
    pending: List[str] = []
    pending_start = 1

    def flush() -> None:
        nonlocal pending
        body = "\n".join(pending).strip("\n")
        if body.strip() and len(body) >= MIN_CHUNK_CHARS:
            chunks.append(
                Chunk(
                    path=path,
                    language=language,
                    start_line=pending_start,
                    end_line=pending_start + len(pending) - 1,
                    text=body,
                )
            )
        pending = []

    for index in range(len(bounds) - 1):
        unit = lines[bounds[index] : bounds[index + 1]]
        if not unit:
            continue
        unit_text = "\n".join(unit)
        unit_start = bounds[index] + 1  # 1-indexed for humans and editors

        if len(unit_text) > MAX_CHUNK_CHARS:
            # Emit whatever was accumulating, then split the oversized unit.
            flush()
            chunks.extend(_hard_split(unit_text, unit_start, path, language))
            pending_start = bounds[index + 1] + 1
            continue

        projected = sum(len(item) + 1 for item in pending) + len(unit_text)
        if pending and projected > MAX_CHUNK_CHARS:
            flush()
            pending_start = unit_start

        if not pending:
            pending_start = unit_start
        pending.extend(unit)

    flush()
    return chunks


def chunk_file(path: Path, *, repo_root: Path) -> List[Chunk]:
    """Read and chunk a file, addressed relative to the repo root.

    Returns an empty list for unindexable suffixes and for anything that isn't
    decodable as UTF-8 — a binary that slipped past the extension allowlist
    should be skipped, not crash a 2,000-file indexing run.
    """
    language = language_for(path)
    if language is None:
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []
    relative = path.relative_to(repo_root).as_posix()
    return chunk_text(text, path=relative, language=language)
