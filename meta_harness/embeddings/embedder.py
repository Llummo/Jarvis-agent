"""Text embedding backends.

`Embedder` is the seam the rest of the package depends on, so indexing and
search can be exercised without a network or an API key.

Two backends. `LocalEmbedder` runs voyage-4-nano (Apache 2.0) on this machine
and is the default: nothing to obtain before indexing works, and no source
code leaves the host. `GeminiEmbedder` calls `gemini-embedding-001` instead,
for when the hosted model is preferred.

Retrieval quality depends on embedding a query and a document differently —
each backend is told which side of the comparison it is producing. Mixing the
two up silently degrades ranking rather than erroring, so the two methods are
deliberately separate.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import List, Optional, Protocol, Sequence

# --- Model limits -----------------------------------------------------------
# These describe gemini-embedding-001 and are the first thing to check if the
# API starts rejecting requests: a limit tightened upstream shows up here as a
# 400, not as a subtle quality drop. Each can be overridden by environment
# variable so a fix needs no code change.
MODEL_NAME = "gemini-embedding-001"

# Per-text input ceiling. Chunking targets a character budget derived from
# this (see chunking.py) rather than counting tokens, which would need the
# model's tokenizer.
MAX_INPUT_TOKENS = int(os.getenv("META_HARNESS_EMBED_MAX_INPUT_TOKENS", "2048"))

# Texts per request. Kept conservative: an oversized batch fails the whole
# request, and the throughput difference above ~32 is small next to the
# network round trip.
MAX_BATCH = int(os.getenv("META_HARNESS_EMBED_MAX_BATCH", "32"))

# gemini-embedding-001 emits 3072 dimensions and supports Matryoshka
# truncation to shorter vectors. 1536 halves storage and index-scan cost for
# a marginal quality loss on retrieval. Truncated vectors are NOT unit-length,
# so everything is normalized on receipt (see `_normalize`) — that makes the
# dot product a cosine regardless of which dimensionality is configured.
DEFAULT_DIMENSIONS = int(os.getenv("META_HARNESS_EMBED_DIMENSIONS", "1536"))

API_KEY_ENV_VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY")

# Task types tell the model which side of a retrieval pair it is embedding.
TASK_DOCUMENT = "RETRIEVAL_DOCUMENT"
TASK_QUERY = "RETRIEVAL_QUERY"
# Use when the query is natural language but the corpus is source code — the
# asymmetry is the point, so this pairs with TASK_DOCUMENT on the corpus side.
TASK_CODE_QUERY = "CODE_RETRIEVAL_QUERY"

MAX_ATTEMPTS = 4
BACKOFF_BASE_S = 1.5

# --- Local backend ----------------------------------------------------------
# voyage-4-nano is open-weight (Apache 2.0) and runs on the machine, so
# indexing works with no API key and no data leaving the host — which matters
# when the thing being indexed is a client's source code.
LOCAL_MODEL_NAME = os.getenv("META_HARNESS_LOCAL_EMBED_MODEL", "voyageai/voyage-4-nano")

# The model emits 2048 dimensions and supports Matryoshka truncation down to
# 256. Storage is not a constraint at repository scale — a few thousand chunks
# is tens of megabytes — so the default keeps the full width and loses nothing.
LOCAL_DIMENSIONS = int(os.getenv("META_HARNESS_LOCAL_EMBED_DIMENSIONS", "2048"))

# The model ships named prompts for the two sides of a retrieval pair; using
# them is what makes query/document asymmetry work.
LOCAL_PROMPT_DOCUMENT = "document"
LOCAL_PROMPT_QUERY = "query"

BACKEND_ENV_VAR = "META_HARNESS_EMBEDDING_BACKEND"
BACKEND_LOCAL = "local"
BACKEND_GEMINI = "gemini"


class EmbeddingError(RuntimeError):
    """Raised when embeddings cannot be produced."""


class Embedder(Protocol):
    """Anything that can turn text into vectors.

    Implementations must return unit-length vectors so callers can treat the
    dot product as a cosine similarity.
    """

    @property
    def dimensions(self) -> int:  # pragma: no cover - structural
        ...

    def embed_documents(self, texts: Sequence[str]) -> List[List[float]]:  # pragma: no cover
        ...

    def embed_query(self, text: str) -> List[float]:  # pragma: no cover
        ...


def _normalize(vector: Sequence[float]) -> List[float]:
    """Scale to unit length so dot product equals cosine similarity.

    Done unconditionally rather than trusting the backend: Matryoshka-
    truncated vectors lose the normalization the full-width ones have, and a
    silently un-normalized vector produces plausible-looking but wrong
    rankings.
    """
    magnitude = sum(value * value for value in vector) ** 0.5
    if magnitude == 0:
        raise EmbeddingError("Embedding backend returned a zero vector, which cannot be normalized")
    return [value / magnitude for value in vector]


@dataclass
class GeminiEmbedder:
    """Embeddings from the Gemini API.

    The client is built lazily so importing this module never requires a
    configured key — the CLI can list and describe an index without one.
    """

    model: str = MODEL_NAME
    output_dimensions: int = DEFAULT_DIMENSIONS
    api_key: Optional[str] = None
    query_task_type: str = TASK_QUERY
    max_batch: int = MAX_BATCH
    _client: object = None

    @property
    def dimensions(self) -> int:
        return self.output_dimensions

    def _resolve_api_key(self) -> str:
        if self.api_key:
            return self.api_key
        for env_var in API_KEY_ENV_VARS:
            value = os.getenv(env_var)
            if value:
                return value
        raise EmbeddingError(
            "No Gemini API key. Set GEMINI_API_KEY in .env "
            f"(also accepted: {', '.join(API_KEY_ENV_VARS[1:])})."
        )

    def _get_client(self):
        if self._client is None:
            try:
                from google import genai
            except ImportError as exc:  # pragma: no cover - dependency is declared
                raise EmbeddingError(
                    "The google-genai package is required for Gemini embeddings. "
                    'Install it with: pip install -e ".[dev]"'
                ) from exc
            self._client = genai.Client(api_key=self._resolve_api_key())
        return self._client

    def _embed(self, texts: Sequence[str], task_type: str) -> List[List[float]]:
        if not texts:
            return []
        from google.genai import types

        client = self._get_client()
        config = types.EmbedContentConfig(
            task_type=task_type,
            output_dimensionality=self.output_dimensions,
            # A single over-long chunk shouldn't fail the batch it happens to
            # land in. Chunking already targets the limit; this is the net.
            auto_truncate=True,
        )

        vectors: List[List[float]] = []
        for start in range(0, len(texts), self.max_batch):
            batch = list(texts[start : start + self.max_batch])
            response = self._call_with_retry(client, batch, config)
            embeddings = response.embeddings or []
            if len(embeddings) != len(batch):
                raise EmbeddingError(
                    f"Gemini returned {len(embeddings)} embeddings for {len(batch)} inputs"
                )
            for embedding in embeddings:
                if not embedding.values:
                    raise EmbeddingError("Gemini returned an embedding with no values")
                vectors.append(_normalize(embedding.values))
        return vectors

    def _call_with_retry(self, client, batch: List[str], config):
        """Retry transient failures; surface bad requests immediately.

        A 4xx means the request itself is wrong (bad key, oversized input) and
        will fail identically on retry — only rate limits and server-side
        faults are worth waiting on.
        """
        last_error: Optional[Exception] = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                return client.models.embed_content(
                    model=self.model, contents=batch, config=config
                )
            except Exception as exc:  # google-genai raises its own error hierarchy
                if not _is_retryable(exc) or attempt == MAX_ATTEMPTS:
                    raise EmbeddingError(f"Gemini embedding request failed: {exc}") from exc
                last_error = exc
                time.sleep(BACKOFF_BASE_S * (2 ** (attempt - 1)))
        raise EmbeddingError(f"Gemini embedding request failed: {last_error}")

    def embed_documents(self, texts: Sequence[str]) -> List[List[float]]:
        return self._embed(texts, TASK_DOCUMENT)

    def embed_query(self, text: str) -> List[float]:
        vectors = self._embed([text], self.query_task_type)
        return vectors[0]


def _is_retryable(exc: Exception) -> bool:
    """Rate limits and server faults are worth another attempt; nothing else is."""
    status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status == 429 or status >= 500
    text = str(exc).lower()
    return any(
        marker in text
        for marker in ("429", "rate limit", "resource_exhausted", "unavailable", "timeout", "503", "500")
    )


# Loading the model costs ~30s and hundreds of megabytes of RAM, so one
# process holds one instance. Keyed by name so an override still works.
_LOCAL_MODELS: dict = {}


@dataclass
class LocalEmbedder:
    """Embeddings from voyage-4-nano, running on this machine.

    The default backend: no API key, no per-token cost, and no source code
    leaving the host. Weights are Apache 2.0 and download once (~700 MB) on
    first use, after which indexing works entirely offline.
    """

    model_name: str = LOCAL_MODEL_NAME
    output_dimensions: int = LOCAL_DIMENSIONS
    batch_size: int = 16

    @property
    def dimensions(self) -> int:
        return self.output_dimensions

    def _model(self):
        cached = _LOCAL_MODELS.get(self.model_name)
        if cached is not None:
            return cached
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise EmbeddingError(
                "Local embeddings need sentence-transformers. Install with:\n"
                '  pip install -e ".[local-embeddings]"\n'
                f"Or use the hosted backend instead: {BACKEND_ENV_VAR}=gemini"
            ) from exc
        try:
            # trust_remote_code: the model is a bidirectional Qwen3 variant and
            # ships its own modelling code, which is how it is distributed.
            model = SentenceTransformer(self.model_name, trust_remote_code=True)
        except Exception as exc:
            raise EmbeddingError(f"Could not load '{self.model_name}': {exc}") from exc
        _LOCAL_MODELS[self.model_name] = model
        return model

    def _encode(self, texts: Sequence[str], prompt_name: str) -> List[List[float]]:
        if not texts:
            return []
        model = self._model()
        try:
            vectors = model.encode(
                list(texts),
                prompt_name=prompt_name,
                batch_size=self.batch_size,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
        except Exception as exc:
            raise EmbeddingError(f"Local embedding failed: {exc}") from exc

        result: List[List[float]] = []
        for vector in vectors:
            values = [float(value) for value in vector]
            if self.output_dimensions < len(values):
                # Matryoshka: the leading dimensions are a usable embedding on
                # their own, but truncating breaks unit length, so renormalize.
                values = _normalize(values[: self.output_dimensions])
            result.append(values)
        return result

    def embed_documents(self, texts: Sequence[str]) -> List[List[float]]:
        return self._encode(texts, LOCAL_PROMPT_DOCUMENT)

    def embed_query(self, text: str) -> List[float]:
        return self._encode([text], LOCAL_PROMPT_QUERY)[0]


def resolve_embedder(backend: Optional[str] = None, **kwargs) -> Embedder:
    """Pick an embedding backend.

    Defaults to the local model so a fresh checkout can index a repository
    without anyone obtaining a key first. Set META_HARNESS_EMBEDDING_BACKEND
    to `gemini` for the hosted alternative.
    """
    choice = (backend or os.getenv(BACKEND_ENV_VAR) or BACKEND_LOCAL).strip().lower()
    if choice == BACKEND_LOCAL:
        return LocalEmbedder(**kwargs)
    if choice == BACKEND_GEMINI:
        return GeminiEmbedder(**kwargs)
    raise EmbeddingError(
        f"Unknown embedding backend {choice!r}. Use '{BACKEND_LOCAL}' or '{BACKEND_GEMINI}'."
    )
