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

import re

from meta_harness.embeddings import (
    KIND_DOCUMENT,
    KIND_GITHUB,
    KIND_REPOSITORY,
    KINDS,
    EmbeddingError,
    clone_github,
    index_document,
    name_from_github_url,
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
        origin=source.display_origin,
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


def _ingest_in_background(kind: str, target: str, name: str, rebuild: bool, token: str) -> None:
    """Fetch and index one source, reporting through `token`.

    Owns its own store: the request that started this has already returned,
    and a SQLite connection must not be shared across threads. Cloning happens
    here too — it is slow enough that doing it in the request would defeat the
    point of a background job.
    """
    store = VectorStore()
    step = lambda message: progress.push(token, message)  # noqa: E731
    try:
        if kind == KIND_DOCUMENT:
            index_document(
                Path(target), embedder=resolve_embedder(), store=store,
                source_name=name, on_step=step,
            )
            progress.finish(token)
            return

        root = Path(target)
        origin = None
        if kind == KIND_GITHUB:
            root = clone_github(target, name=name, on_step=step)
            origin = target

        store.sources.register(name, root, kind=kind, origin=origin)
        result = index_repository(
            root,
            embedder=resolve_embedder(),
            store=store,
            repo_name=name,
            rebuild=rebuild,
            on_step=step,
        )
        # Indexing re-registers as a plain repository; restore the real kind
        # and origin so a clone is not mistaken for a local directory.
        store.sources.register(name, root, kind=kind, origin=origin)
        store.sources.mark_indexed(name)
        if result.errors:
            progress.push(token, f"{len(result.errors)} file(s) failed: {result.errors[0]}")
        progress.finish(token)
    except Exception as exc:  # a background thread has nowhere else to report
        progress.push(token, f"Failed: {exc}")
        progress.finish(token, error=str(exc))
    finally:
        store.close()


@router.post("", response_model=AddSourceOut)
def add_source(body: AddSourceIn) -> AddSourceOut:
    """Register a source and start ingesting it.

    Validation that can be done instantly happens here so a typo comes back as
    an error on the request; anything slow (cloning, reading, embedding) moves
    to the background job.
    """
    target = body.target.strip()
    if not target:
        raise HTTPException(status_code=400, detail="Give a path or a repository URL.")
    if body.kind not in KINDS:
        raise HTTPException(status_code=400, detail=f"Unknown source kind: {body.kind}")

    if body.kind == KIND_GITHUB:
        if not re.match(r"^(https?://|git@)", target):
            raise HTTPException(
                status_code=400,
                detail="A repository URL should start with https:// or git@.",
            )
        default_name = name_from_github_url(target)
    else:
        path = Path(target).expanduser()
        if body.kind == KIND_DOCUMENT and not path.is_file():
            raise HTTPException(status_code=400, detail=f"No such file: {path}")
        if body.kind == KIND_REPOSITORY and not path.is_dir():
            raise HTTPException(status_code=400, detail=f"No such directory: {path}")
        target = str(path)
        default_name = path.resolve().stem if body.kind == KIND_DOCUMENT else path.resolve().name

    name = (body.name or default_name).strip()
    if not name:
        raise HTTPException(status_code=400, detail="Give the source a name.")

    token = body.progress_token or uuid.uuid4().hex
    progress.start(token)
    progress.push(token, f"Starting on {target}…")
    threading.Thread(
        target=_ingest_in_background,
        args=(body.kind, target, name, body.rebuild, token),
        daemon=True,
    ).start()

    return AddSourceOut(
        name=name, kind=body.kind, origin=target, indexing=True, progress_token=token
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
