"""SQLite-backed vector store.

A dedicated vector database would be the wrong call at this scale. A repo the
size of the ones this indexes produces vectors in the thousands, and a numpy
dot product over a few thousand rows is sub-millisecond — the operational cost
of another service would buy nothing.

Vectors are stored as float32 blobs: half the size of float64, and well below
the precision that matters once vectors are unit-length.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from meta_harness.embeddings.chunking import Chunk
from meta_harness.embeddings.sources import SourceRegistry

SCHEMA = """
CREATE TABLE IF NOT EXISTS indexed_files (
    repo TEXT NOT NULL,
    path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    chunk_count INTEGER NOT NULL,
    indexed_at TEXT NOT NULL,
    PRIMARY KEY (repo, path)
);

CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo TEXT NOT NULL,
    path TEXT NOT NULL,
    language TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    text TEXT NOT NULL,
    dimensions INTEGER NOT NULL,
    vector BLOB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chunks_repo ON chunks(repo);
CREATE INDEX IF NOT EXISTS idx_chunks_repo_path ON chunks(repo, path);
"""

DEFAULT_DB_NAME = "embeddings.db"

# Added after the first databases were written; SQLite has no
# ADD COLUMN IF NOT EXISTS, so this is applied defensively on open.
CHUNK_MIGRATIONS = ("ALTER TABLE chunks ADD COLUMN mode TEXT DEFAULT 'full'",)

MODE_FULL = "full"
MODE_SKELETON = "skeleton"


class VectorStoreError(RuntimeError):
    """Raised when the store cannot satisfy a request."""


@dataclass(frozen=True)
class SearchHit:
    """One retrieved chunk, where it came from, and how well it matched."""

    path: str
    language: str
    start_line: int
    end_line: int
    text: str
    score: float
    repo: str = ""
    # How the chunk was found, and whether it still matches the file on disk.
    # `unchecked` is the honest default: verification costs a file read, so it
    # is opt-in rather than silently assumed.
    retrieval: str = "semantic"
    verification: str = "unchecked"
    # How the chunk was indexed. A `skeleton` chunk holds declarations only,
    # and is expanded back to the real code from disk before it is returned.
    mode: str = MODE_FULL

    @property
    def location(self) -> str:
        """`path:line` — clickable in most terminals and editors."""
        return f"{self.path}:{self.start_line}"

    @property
    def citation(self) -> str:
        """A reference a reader can follow back to the source."""
        return f"{self.repo}/{self.path}:{self.start_line}-{self.end_line}" if self.repo else self.location


def default_db_path() -> Path:
    """Where the index lives by default.

    Alongside the QA database, under the gitignored `qa/` directory — an index
    contains verbatim source code and must never be committed.
    """
    return Path(__file__).resolve().parent.parent.parent / "qa" / DEFAULT_DB_NAME


class VectorStore:
    """Chunk vectors for one or more repositories."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path) if db_path is not None else default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(self.db_path))
        self._connection.row_factory = sqlite3.Row
        self._connection.executescript(SCHEMA)
        for statement in CHUNK_MIGRATIONS:
            try:
                self._connection.execute(statement)
            except Exception:
                pass  # already applied
        self._connection.commit()
        # Registered origins for everything in this database. Sharing the
        # connection keeps chunks and their provenance in one transaction
        # boundary — deleting one without the other would leave either
        # orphaned vectors or results that cannot be attributed.
        self.sources = SourceRegistry(self._connection)

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "VectorStore":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- writing ------------------------------------------------------------

    def file_hashes(self, repo: str) -> Dict[str, str]:
        """Known content hash per path, for deciding what needs re-embedding."""
        rows = self._connection.execute(
            "SELECT path, content_hash FROM indexed_files WHERE repo = ?", (repo,)
        ).fetchall()
        return {row["path"]: row["content_hash"] for row in rows}

    def replace_file(
        self,
        repo: str,
        path: str,
        content_hash: str,
        chunks: Sequence[Chunk],
        vectors: Sequence[Sequence[float]],
        mode: str = MODE_FULL,
    ) -> None:
        """Swap in a file's chunks atomically.

        Delete-then-insert in one transaction: a crash midway would otherwise
        leave a file indexed under a stale hash with no chunks behind it, and
        the next run would consider it up to date.
        """
        if len(chunks) != len(vectors):
            raise VectorStoreError(
                f"{len(chunks)} chunks but {len(vectors)} vectors for {path}"
            )
        now = datetime.now(timezone.utc).isoformat()
        with self._connection:
            self._connection.execute(
                "DELETE FROM chunks WHERE repo = ? AND path = ?", (repo, path)
            )
            self._connection.executemany(
                "INSERT INTO chunks (repo, path, language, start_line, end_line, text, dimensions, vector, mode) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        repo,
                        chunk.path,
                        chunk.language,
                        chunk.start_line,
                        chunk.end_line,
                        chunk.text,
                        len(vector),
                        np.asarray(vector, dtype=np.float32).tobytes(),
                        mode,
                    )
                    for chunk, vector in zip(chunks, vectors)
                ],
            )
            self._connection.execute(
                "INSERT INTO indexed_files (repo, path, content_hash, chunk_count, indexed_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(repo, path) DO UPDATE SET "
                "content_hash = excluded.content_hash, chunk_count = excluded.chunk_count, "
                "indexed_at = excluded.indexed_at",
                (repo, path, content_hash, len(chunks), now),
            )

    def forget_paths(self, repo: str, paths: Sequence[str]) -> int:
        """Drop files that no longer exist in the working tree.

        Without this, deleted and renamed files keep being retrieved forever,
        and the model is handed code that isn't there any more.
        """
        if not paths:
            return 0
        with self._connection:
            for path in paths:
                self._connection.execute(
                    "DELETE FROM chunks WHERE repo = ? AND path = ?", (repo, path)
                )
                self._connection.execute(
                    "DELETE FROM indexed_files WHERE repo = ? AND path = ?", (repo, path)
                )
        return len(paths)

    def clear_repo(self, repo: str) -> None:
        with self._connection:
            self._connection.execute("DELETE FROM chunks WHERE repo = ?", (repo,))
            self._connection.execute("DELETE FROM indexed_files WHERE repo = ?", (repo,))

    # -- reading ------------------------------------------------------------

    def repos(self) -> List[Tuple[str, int, int]]:
        """(repo, file count, chunk count) for everything indexed."""
        rows = self._connection.execute(
            "SELECT f.repo AS repo, COUNT(DISTINCT f.path) AS files, "
            "COALESCE(SUM(f.chunk_count), 0) AS chunks "
            "FROM indexed_files f GROUP BY f.repo ORDER BY f.repo"
        ).fetchall()
        return [(row["repo"], row["files"], row["chunks"]) for row in rows]

    def _load_matrix(self, repo: str, dimensions: int) -> Tuple[np.ndarray, List[sqlite3.Row]]:
        rows = self._connection.execute(
            "SELECT path, language, start_line, end_line, text, dimensions, vector, "
            "COALESCE(mode, 'full') AS mode FROM chunks WHERE repo = ?",
            (repo,),
        ).fetchall()
        if not rows:
            raise VectorStoreError(
                f"Nothing indexed for repo '{repo}'. Run: meta-harness index build --repo <path>"
            )

        # Every row must share a width. Trusting the first one and reshaping
        # blind turns a mixed index into an opaque numpy error hundreds of
        # lines from the cause — and an index does get mixed in practice, by
        # two jobs writing the same source with different settings.
        widths = {row["dimensions"] for row in rows}
        if len(widths) > 1:
            counts = ", ".join(
                f"{w}d x{sum(1 for r in rows if r['dimensions'] == w)}" for w in sorted(widths)
            )
            raise VectorStoreError(
                f"Index for '{repo}' holds vectors of different widths ({counts}), which means "
                "it was written by more than one embedding backend. Rebuild it with: "
                "meta-harness index build --repo <path> --rebuild"
            )

        stored_dimensions = widths.pop()
        if stored_dimensions != dimensions:
            raise VectorStoreError(
                f"Index for '{repo}' holds {stored_dimensions}-dimensional vectors but the "
                f"embedder produces {dimensions}. The index was built with different settings — "
                "rebuild it with: meta-harness index build --repo <path> --rebuild"
            )

        matrix = np.frombuffer(
            b"".join(row["vector"] for row in rows), dtype=np.float32
        ).reshape(len(rows), stored_dimensions)
        return matrix, rows

    def search(
        self,
        repo: str,
        query_vector: Sequence[float],
        *,
        limit: int = 10,
        min_score: float = 0.0,
    ) -> List[SearchHit]:
        """Rank chunks by cosine similarity to the query.

        Vectors are unit-length on both sides, so the dot product is the
        cosine — no division, and the score is directly comparable across
        queries, which is what makes a threshold meaningful.
        """
        query = np.asarray(query_vector, dtype=np.float32)
        matrix, rows = self._load_matrix(repo, len(query))

        scores = matrix @ query
        # argpartition beats a full sort once the corpus is large and only the
        # top handful is wanted.
        count = min(limit, len(rows))
        top = np.argpartition(-scores, count - 1)[:count]
        top = top[np.argsort(-scores[top])]

        hits: List[SearchHit] = []
        for position in top:
            score = float(scores[position])
            if score < min_score:
                continue
            row = rows[position]
            hits.append(
                SearchHit(
                    path=row["path"],
                    language=row["language"],
                    start_line=row["start_line"],
                    end_line=row["end_line"],
                    text=row["text"],
                    score=score,
                    repo=repo,
                    mode=row["mode"],
                )
            )
        return hits
