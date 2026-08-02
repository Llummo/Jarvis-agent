"""Declared context sources, and proof that what they return is real.

Retrieval is only worth trusting if you can say where a span came from and
confirm it is still there. A registered source records what was indexed, from
which directory, and at which git revision; verification re-reads the span
from disk and compares it to what the index holds.

That distinction matters. An index is a copy, and a copy goes stale silently:
a file edited after indexing still returns its old contents, and nothing about
the result looks wrong. Verification turns "this is probably the code" into
"this is the code, and here is the line it is on".
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

SOURCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    name TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    root TEXT NOT NULL,
    revision TEXT,
    registered_at TEXT NOT NULL,
    indexed_at TEXT
);
"""

# `origin` arrived after the first databases were written, and
# CREATE TABLE IF NOT EXISTS will not add a column to an existing table.
SOURCE_MIGRATIONS = ("ALTER TABLE sources ADD COLUMN origin TEXT",)

# What a source is, which decides how it gets indexed.
KIND_REPOSITORY = "repository"   # a directory on this machine
KIND_GITHUB = "github"           # a remote repo, cloned into a local cache
KIND_DOCUMENT = "document"       # a single file: markdown, text or PDF

KINDS = (KIND_REPOSITORY, KIND_GITHUB, KIND_DOCUMENT)

# Verification outcomes for a single retrieved span.
VERIFIED = "verified"
DRIFTED = "drifted"
MISSING = "missing"
UNCHECKED = "unchecked"


class SourceError(RuntimeError):
    """Raised when a source cannot be registered or resolved."""


@dataclass(frozen=True)
class Source:
    """A registered, trusted origin for retrieved context."""

    name: str
    kind: str
    root: Path
    revision: Optional[str]
    registered_at: str
    indexed_at: Optional[str]
    # Where it came from, when that differs from where it now lives: the clone
    # URL for a GitHub source, the original path for a document. Kept so the
    # citation can name the real origin rather than a cache directory.
    origin: Optional[str] = None

    @property
    def exists(self) -> bool:
        return self.root.is_dir() if self.kind != KIND_DOCUMENT else self.root.is_file()

    @property
    def display_origin(self) -> str:
        return self.origin or str(self.root)


@dataclass(frozen=True)
class SpanCheck:
    """Whether one retrieved span still matches the file on disk."""

    status: str
    detail: str = ""

    @property
    def trustworthy(self) -> bool:
        return self.status == VERIFIED


def git_revision(root: Path) -> Optional[str]:
    """The current commit, or None when this isn't a git repo.

    Recorded at index time so a stale index can be identified as stale rather
    than merely suspected.
    """
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    revision = completed.stdout.strip()
    return revision or None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SourceRegistry:
    """The set of sources the harness is allowed to draw context from.

    Shares the index database, because a source and its chunks are meaningless
    apart — deleting one without the other leaves either orphaned vectors or
    unattributable results.
    """

    def __init__(self, connection):
        self._connection = connection
        self._connection.executescript(SOURCE_SCHEMA)
        for statement in SOURCE_MIGRATIONS:
            try:
                self._connection.execute(statement)
            except Exception:
                # Already applied. SQLite has no ADD COLUMN IF NOT EXISTS, and
                # checking pragma output first is more code for the same result.
                pass
        self._connection.commit()

    def register(
        self,
        name: str,
        root: Path,
        *,
        kind: str = KIND_REPOSITORY,
        revision: Optional[str] = None,
        origin: Optional[str] = None,
    ) -> Source:
        """Declare a source as trusted. Re-registering updates it in place."""
        if kind not in KINDS:
            raise SourceError(f"Unknown source kind {kind!r}; must be one of {KINDS}")
        root = Path(root).expanduser().resolve()
        if kind == KIND_DOCUMENT:
            if not root.is_file():
                raise SourceError(f"Not a file: {root}")
        elif not root.is_dir():
            raise SourceError(f"Not a directory: {root}")
        if not name.strip():
            raise SourceError("A source needs a name")

        search_root = root if kind != KIND_DOCUMENT else root.parent
        resolved_revision = revision if revision is not None else git_revision(search_root)
        with self._connection:
            self._connection.execute(
                "INSERT INTO sources (name, kind, root, revision, registered_at, indexed_at, origin) "
                "VALUES (?, ?, ?, ?, ?, NULL, ?) "
                "ON CONFLICT(name) DO UPDATE SET "
                "kind = excluded.kind, root = excluded.root, revision = excluded.revision, "
                "origin = excluded.origin",
                (name.strip(), kind, str(root), resolved_revision, _now(), origin),
            )
        return self.get(name.strip())

    def mark_indexed(self, name: str, *, revision: Optional[str] = None) -> None:
        """Record that the index now reflects this revision of the source."""
        with self._connection:
            if revision is None:
                self._connection.execute(
                    "UPDATE sources SET indexed_at = ? WHERE name = ?", (_now(), name)
                )
            else:
                self._connection.execute(
                    "UPDATE sources SET indexed_at = ?, revision = ? WHERE name = ?",
                    (_now(), revision, name),
                )

    def get(self, name: str) -> Source:
        row = self._connection.execute(
            "SELECT name, kind, root, revision, registered_at, indexed_at, origin FROM sources WHERE name = ?",
            (name,),
        ).fetchone()
        if row is None:
            raise SourceError(
                f"'{name}' is not a registered source. "
                "Add it with: meta-harness source add --repo <path>"
            )
        return Source(
            name=row["name"],
            kind=row["kind"],
            root=Path(row["root"]),
            revision=row["revision"],
            registered_at=row["registered_at"],
            indexed_at=row["indexed_at"],
            origin=row["origin"],
        )

    def find(self, name: str) -> Optional[Source]:
        try:
            return self.get(name)
        except SourceError:
            return None

    def list_sources(self) -> List[Source]:
        rows = self._connection.execute(
            "SELECT name, kind, root, revision, registered_at, indexed_at, origin FROM sources ORDER BY name"
        ).fetchall()
        return [
            Source(
                name=row["name"],
                kind=row["kind"],
                root=Path(row["root"]),
                revision=row["revision"],
                registered_at=row["registered_at"],
                indexed_at=row["indexed_at"],
                origin=row["origin"],
            )
            for row in rows
        ]

    def remove(self, name: str) -> bool:
        with self._connection:
            cursor = self._connection.execute("DELETE FROM sources WHERE name = ?", (name,))
        return cursor.rowcount > 0


def resolve_file(source: Source, relative: str) -> Path:
    """The file on disk that an indexed path refers to.

    A repository's root is a directory and indexed paths hang off it. A
    document's root is the file itself, so joining would look for the file
    inside itself and report every span as missing.
    """
    if source.kind == KIND_DOCUMENT:
        return source.root
    return source.root / relative


def check_span(source: Source, path: str, start_line: int, end_line: int, text: str) -> SpanCheck:
    """Confirm a retrieved span still matches the file on disk.

    Compares non-blank lines rather than raw text: an index stores a chunk
    stripped of surrounding blank lines, and re-reading by line range picks up
    whitespace that was never a difference worth reporting.
    """
    target = resolve_file(source, path)
    try:
        if not target.is_file():
            return SpanCheck(MISSING, f"{path} no longer exists in {source.name}")
        lines = target.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        return SpanCheck(MISSING, f"{path} could not be read: {exc}")

    if start_line < 1 or end_line > len(lines):
        return SpanCheck(
            DRIFTED,
            f"{path} is now {len(lines)} lines; the index cites {start_line}-{end_line}",
        )

    on_disk = [line.strip() for line in lines[start_line - 1 : end_line] if line.strip()]
    indexed = [line.strip() for line in text.splitlines() if line.strip()]
    if on_disk == indexed:
        return SpanCheck(VERIFIED, f"{path}:{start_line} matches the working tree")
    return SpanCheck(DRIFTED, f"{path}:{start_line} has changed since it was indexed")


@dataclass
class SourceStatus:
    """How far an indexed source has drifted from its working tree."""

    source: Source
    indexed_revision: Optional[str]
    current_revision: Optional[str]
    files_indexed: int
    files_changed: int
    files_removed: int

    @property
    def current(self) -> bool:
        return self.files_changed == 0 and self.files_removed == 0

    def summary(self) -> str:
        if not self.source.exists:
            return f"missing — {self.source.root} is not a directory"
        if self.source.indexed_at is None:
            return "registered but never indexed"
        if self.current:
            return f"current — {self.files_indexed} file(s) verified"
        parts = []
        if self.files_changed:
            parts.append(f"{self.files_changed} changed")
        if self.files_removed:
            parts.append(f"{self.files_removed} removed")
        return "stale — " + ", ".join(parts) + " since indexing"
