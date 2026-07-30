"""Index a repository into the vector store.

File discovery goes through `git ls-files` when the target is a git
repository. That is not a convenience — it is what makes indexing a real
project tractable. A 3 GB checkout is mostly `node_modules`, build output and
images; the tracked file list is the source, and it comes pre-filtered by the
project's own `.gitignore` with no exclude list to maintain here.

Re-indexing is incremental: a file whose content hash is unchanged is skipped
without being read or embedded, so the second run over an unchanged repo costs
one `git ls-files` and a hash per file.
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from meta_harness.embeddings.chunking import Chunk, chunk_file, language_for
from meta_harness.embeddings.embedder import Embedder
from meta_harness.embeddings.store import VectorStore

OnStep = Optional[Callable[[str], None]]

# Directories to skip when the target isn't a git repo and there's no
# .gitignore to lean on.
FALLBACK_EXCLUDES = {
    ".git", "node_modules", "vendor", "dist", "build", ".next", "out",
    "__pycache__", ".venv", "venv", "env", "target", ".idea", ".vscode",
    "coverage", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache",
}

# Files above this are generated — minified bundles, lockfiles, fixtures.
# Indexing them buys nothing and costs a lot of tokens.
MAX_FILE_BYTES = 400_000


def _report(on_step: OnStep, message: str) -> None:
    if on_step is not None:
        on_step(message)


@dataclass
class IndexResult:
    """What one indexing run actually did."""

    repo: str
    files_scanned: int = 0
    files_embedded: int = 0
    files_skipped_unchanged: int = 0
    files_removed: int = 0
    chunks_embedded: int = 0
    errors: List[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [
            f"{self.files_embedded} file(s) embedded",
            f"{self.chunks_embedded} chunk(s)",
            f"{self.files_skipped_unchanged} unchanged",
        ]
        if self.files_removed:
            parts.append(f"{self.files_removed} removed")
        if self.errors:
            parts.append(f"{len(self.errors)} error(s)")
        return ", ".join(parts)


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _git_tracked_files(repo_root: Path) -> Optional[List[Path]]:
    """Files git knows about, or None when this isn't a git repo.

    `--others --exclude-standard` alongside `--cached` matters: without it,
    only *committed* files are listed, so a developer indexing mid-feature
    silently gets no results for the code they are actually working on.
    `--exclude-standard` still honours `.gitignore`, which is the whole reason
    for going through git rather than walking the tree.

    `-z` because real projects contain paths with spaces, and a newline-split
    would corrupt them.
    """
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    names = [name for name in completed.stdout.decode("utf-8", "replace").split("\0") if name]
    return [repo_root / name for name in names]


def _walked_files(repo_root: Path) -> List[Path]:
    """Every candidate file, skipping known-generated directories."""
    found: List[Path] = []
    for path in repo_root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in FALLBACK_EXCLUDES for part in path.relative_to(repo_root).parts):
            continue
        found.append(path)
    return found


def discover_files(repo_root: Path) -> List[Path]:
    """Indexable source files in the repo, in a stable order."""
    candidates = _git_tracked_files(repo_root)
    if candidates is None:
        candidates = _walked_files(repo_root)

    indexable: List[Path] = []
    for path in candidates:
        if language_for(path) is None:
            continue
        try:
            if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        indexable.append(path)
    return sorted(indexable)


def index_repository(
    repo_root: Path,
    *,
    embedder: Embedder,
    store: VectorStore,
    repo_name: Optional[str] = None,
    rebuild: bool = False,
    on_step: OnStep = None,
) -> IndexResult:
    """Embed a repository's source into `store`.

    One file's failure never stops the run — a single undecodable or oversized
    file in a 2,000-file repo should cost that file, not the index.
    """
    repo_root = Path(repo_root).expanduser().resolve()
    if not repo_root.is_dir():
        raise NotADirectoryError(f"Not a directory: {repo_root}")

    name = repo_name or repo_root.name
    result = IndexResult(repo=name)

    if rebuild:
        _report(on_step, f"Clearing the existing index for '{name}'…")
        store.clear_repo(name)

    _report(on_step, f"Discovering source files in {repo_root}…")
    files = discover_files(repo_root)
    result.files_scanned = len(files)
    _report(on_step, f"{len(files)} indexable file(s) found.")

    known = store.file_hashes(name)
    seen: set = set()

    for position, path in enumerate(files, start=1):
        relative = path.relative_to(repo_root).as_posix()
        seen.add(relative)
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue

        digest = content_hash(text)
        if known.get(relative) == digest:
            result.files_skipped_unchanged += 1
            continue

        chunks: List[Chunk] = chunk_file(path, repo_root=repo_root)
        if not chunks:
            # An empty or comment-only file still gets its hash recorded, so
            # the next run doesn't re-read it every time.
            store.replace_file(name, relative, digest, [], [])
            continue

        _report(
            on_step,
            f"[{position}/{len(files)}] Embedding {relative} ({len(chunks)} chunk(s))…",
        )
        try:
            vectors = embedder.embed_documents([chunk.embed_text() for chunk in chunks])
        except Exception as exc:
            result.errors.append(f"{relative}: {exc}")
            continue

        store.replace_file(name, relative, digest, chunks, vectors)
        result.files_embedded += 1
        result.chunks_embedded += len(chunks)

    # Anything indexed previously but absent now was deleted or renamed.
    stale = [path for path in known if path not in seen]
    if stale:
        result.files_removed = store.forget_paths(name, stale)
        _report(on_step, f"Dropped {result.files_removed} file(s) no longer in the repo.")

    _report(on_step, f"Done: {result.summary()}")
    return result


def search_repository(
    query: str,
    *,
    repo: str,
    embedder: Embedder,
    store: VectorStore,
    limit: int = 10,
    min_score: float = 0.0,
) -> List:
    """Retrieve the chunks most similar to a natural-language query."""
    if not query.strip():
        raise ValueError("query is required")
    vector = embedder.embed_query(query.strip())
    return store.search(repo, vector, limit=limit, min_score=min_score)
