"""Repository embeddings: real context retrieved from source, not pasted in.

The harness previously could only reason about a codebase if a human pasted
the relevant documentation into a form field. This package indexes a
repository's source once and retrieves the relevant spans on demand, so the
context is produced by the harness rather than supplied to it.
"""

from meta_harness.embeddings.chunking import Chunk, chunk_file, chunk_text, language_for
from meta_harness.embeddings.embedder import (
    DEFAULT_DIMENSIONS,
    LOCAL_MODEL_NAME,
    MODEL_NAME,
    Embedder,
    EmbeddingError,
    GeminiEmbedder,
    LocalEmbedder,
    resolve_embedder,
)
from meta_harness.embeddings.indexer import (
    IndexResult,
    discover_files,
    hybrid_search,
    index_repository,
    search_repository,
    source_status,
    verify_hits,
)
from meta_harness.embeddings.lexical import (
    LexicalHit,
    available_backend,
    find_symbol,
    looks_like_symbol,
)
from meta_harness.embeddings.sources import (
    DRIFTED,
    MISSING,
    UNCHECKED,
    VERIFIED,
    Source,
    SourceError,
    SourceRegistry,
    SourceStatus,
)
from meta_harness.embeddings.store import SearchHit, VectorStore, VectorStoreError, default_db_path

__all__ = [
    "Chunk",
    "DRIFTED",
    "LexicalHit",
    "MISSING",
    "Source",
    "SourceError",
    "SourceRegistry",
    "SourceStatus",
    "UNCHECKED",
    "VERIFIED",
    "available_backend",
    "find_symbol",
    "hybrid_search",
    "looks_like_symbol",
    "source_status",
    "verify_hits",
    "DEFAULT_DIMENSIONS",
    "Embedder",
    "EmbeddingError",
    "GeminiEmbedder",
    "LOCAL_MODEL_NAME",
    "LocalEmbedder",
    "IndexResult",
    "MODEL_NAME",
    "SearchHit",
    "VectorStore",
    "VectorStoreError",
    "chunk_file",
    "chunk_text",
    "default_db_path",
    "discover_files",
    "index_repository",
    "language_for",
    "resolve_embedder",
    "search_repository",
]
