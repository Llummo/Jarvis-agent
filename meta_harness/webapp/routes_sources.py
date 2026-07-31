"""Managing the repositories the harness may draw context from.

Indexing is the awkward part of this surface: it runs for minutes on a small
project and hours on a large one, which is far longer than an HTTP request
should be held open. So the POST registers the source, starts the work on a
background thread, and returns a progress token the client polls. That is the
same tracker the other long operations use, extended with a terminal state so
the UI knows when to stop asking.

A plain daemon thread is the right weight here, matching progress.py's own
reasoning: this is a single-user local tool, not a multi-worker deployment.
"""

from __future__ import annotations

import threading
import uuid
from pathlib import Path
from typing import List

from fastapi import APIRouter, HTTPException

from meta_harness.embeddings import (
    EmbeddingError,
    SourceError,
    VectorStore,
    VectorStoreError,
    available_backend,
    hybrid_search,
    index_repository,
    resolve_embedder,
    source_status,
)
from meta_harness.embeddings.embedder import BACKEND_ENV_VAR, BACKEND_LOCAL
from meta_harness.webapp import progress
from meta_harness.webapp.schemas import (
    AddSourceIn,
    AddSourceOut,
    SearchHitOut,
    SearchSourceIn,
    SearchSourceOut,
    SourceOut,
    SourcesOut,
)

router = APIRouter()


def _chunk_counts(store: VectorStore) -> dict:
    return {name: chunks for name, _files, chunks in store.repos()}


def _describe(store: VectorStore, name: str, chunks: dict) -> SourceOut:
    source = store.sources.get(name)
    try:
        status = source_status(store, name)
    except SourceError:
        status = None
    return SourceOut(
        name=source.name,
        root=str(source.root),
        kind=source.kind,
        revision=(source.revision or "")[:8],
        indexed=source.indexed_at is not None,
        status=status.summary() if status else "unknown",
        current=bool(status and status.current and source.exists),
        files_indexed=status.files_indexed if status else 0,
        files_changed=status.files_changed if status else 0,
        files_removed=status.files_removed if status else 0,
        chunks=chunks.get(name, 0),
    )


@router.get("", response_model=SourcesOut)
def list_sources() -> SourcesOut:
    """Every registered source, with how far its index has drifted."""
    import os

    store = VectorStore()
    try:
        chunks = _chunk_counts(store)
        described: List[SourceOut] = [
            _describe(store, source.name, chunks) for source in store.sources.list_sources()
        ]
    finally:
        store.close()
    return SourcesOut(
        sources=described,
        lexical_backend=available_backend(),
        embedding_backend=(os.getenv(BACKEND_ENV_VAR) or BACKEND_LOCAL).strip().lower(),
    )


def _index_in_background(path: Path, name: str, rebuild: bool, token: str) -> None:
    """Run one indexing job to completion, reporting through `token`.

    Owns its own store: the request that started this has already returned,
    and a SQLite connection must not be shared across threads.
    """
    store = VectorStore()
    try:
        result = index_repository(
            path,
            embedder=resolve_embedder(),
            store=store,
            repo_name=name,
            rebuild=rebuild,
            on_step=lambda message: progress.push(token, message),
        )
        if result.errors:
            progress.push(token, f"{len(result.errors)} file(s) failed: {result.errors[0]}")
        progress.finish(token)
    except Exception as exc:  # a background thread has nowhere else to report
        progress.push(token, f"Indexing failed: {exc}")
        progress.finish(token, error=str(exc))
    finally:
        store.close()


@router.post("", response_model=AddSourceOut)
def add_source(body: AddSourceIn) -> AddSourceOut:
    """Register a repository and start indexing it."""
    path = Path(body.path).expanduser()
    if not path.is_dir():
        raise HTTPException(status_code=400, detail=f"Not a directory: {path}")

    name = (body.name or path.resolve().name).strip()
    store = VectorStore()
    try:
        source = store.sources.register(name, path)
    except SourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        store.close()

    if not body.index:
        return AddSourceOut(name=source.name, root=str(source.root))

    token = body.progress_token or uuid.uuid4().hex
    progress.start(token)
    progress.push(token, f"Indexing {source.root}…")
    threading.Thread(
        target=_index_in_background,
        args=(path, name, body.rebuild, token),
        daemon=True,
    ).start()

    return AddSourceOut(
        name=source.name, root=str(source.root), indexing=True, progress_token=token
    )


@router.post("/{name}/verify", response_model=SourceOut)
def verify_source(name: str) -> SourceOut:
    """Re-check a source's index against what is on disk."""
    store = VectorStore()
    try:
        chunks = _chunk_counts(store)
        return _describe(store, name, chunks)
    except SourceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        store.close()


@router.delete("/{name}", response_model=SourcesOut)
def remove_source(name: str) -> SourcesOut:
    """Deregister a source and drop everything indexed from it."""
    store = VectorStore()
    try:
        if not store.sources.remove(name):
            raise HTTPException(status_code=404, detail=f"'{name}' is not a registered source.")
        store.clear_repo(name)
    finally:
        store.close()
    return list_sources()


@router.post("/search", response_model=SearchSourceOut)
def search_source(body: SearchSourceIn) -> SearchSourceOut:
    """Retrieve from a source, so the context can be inspected before it is
    trusted to drive a verdict."""
    if not body.query.strip():
        raise HTTPException(status_code=400, detail="A query is required")

    store = VectorStore()
    try:
        hits = hybrid_search(
            body.query,
            repo=body.repo,
            embedder=resolve_embedder(),
            store=store,
            limit=body.limit,
            verify=body.verify,
        )
    except (VectorStoreError, SourceError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except EmbeddingError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        store.close()

    return SearchSourceOut(
        hits=[
            SearchHitOut(
                path=hit.path,
                language=hit.language,
                start_line=hit.start_line,
                end_line=hit.end_line,
                text=hit.text,
                score=hit.score,
                retrieval=hit.retrieval,
                verification=hit.verification,
                citation=hit.citation,
            )
            for hit in hits
        ]
    )
