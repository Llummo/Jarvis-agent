"""Repository embeddings: chunking, storage, indexing and search.

The embedder is injected everywhere, so the whole pipeline is exercised here
without a network or an API key. `FakeEmbedder` hashes tokens into a bag of
words — meaningless semantically, but it produces real lexical similarity,
which is what makes the ranking assertions below meaningful rather than
tautological.
"""

from __future__ import annotations

import hashlib
import math
import re
import subprocess

import pytest

from meta_harness.embeddings.chunking import (
    MAX_CHUNK_CHARS,
    MIN_CHUNK_CHARS,
    Chunk,
    chunk_file,
    chunk_text,
    language_for,
)
from meta_harness.embeddings.embedder import (
    EmbeddingError,
    GeminiEmbedder,
    _is_retryable,
    _normalize,
)
from meta_harness.embeddings.indexer import (
    content_hash,
    discover_files,
    index_repository,
    search_repository,
)
from meta_harness.embeddings.store import VectorStore, VectorStoreError

DIMS = 128


class FakeEmbedder:
    """Deterministic bag-of-words vectors, unit length, no network."""

    dimensions = DIMS

    def __init__(self):
        self.document_calls = 0
        self.query_calls = 0

    def _vector(self, text: str):
        values = [0.0] * DIMS
        for token in re.findall(r"[A-Za-z_]{3,}", text.lower()):
            index = int(hashlib.md5(token.encode()).hexdigest(), 16) % DIMS
            values[index] += 1.0
        magnitude = math.sqrt(sum(v * v for v in values)) or 1.0
        return [v / magnitude for v in values]

    def embed_documents(self, texts):
        self.document_calls += 1
        return [self._vector(text) for text in texts]

    def embed_query(self, text):
        self.query_calls += 1
        return self._vector(text)


class ExplodingEmbedder(FakeEmbedder):
    """Fails on one named file, to prove per-file error isolation."""

    def __init__(self, fail_on: str):
        super().__init__()
        self.fail_on = fail_on

    def embed_documents(self, texts):
        if any(self.fail_on in text for text in texts):
            raise EmbeddingError("simulated backend failure")
        return super().embed_documents(texts)


@pytest.fixture
def store(tmp_path):
    with VectorStore(tmp_path / "embeddings.db") as running:
        yield running


@pytest.fixture
def repo(tmp_path):
    """A small git repo, since discovery goes through `git ls-files`."""
    root = tmp_path / "sample"
    (root / "internal").mkdir(parents=True)
    (root / "internal" / "people.go").write_text(
        "package people\n\n"
        "// Employee is a person on the payroll.\n"
        "type Employee struct {\n\tID string\n\tName string\n}\n\n"
        "func CreateEmployee(name string) (*Employee, error) {\n"
        "\treturn &Employee{Name: name}, nil\n}\n",
        encoding="utf-8",
    )
    (root / "billing.ts").write_text(
        "export interface Invoice {\n  id: string;\n  total: number;\n}\n\n"
        "export function calculateInvoiceTotal(items: number[]): number {\n"
        "  return items.reduce((a, b) => a + b, 0);\n}\n",
        encoding="utf-8",
    )
    (root / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 200)
    (root / "README.md").write_text(
        "# Sample\n\nA sample project used by the embedding tests. It exists so the\n"
        "indexer has a markdown file with enough prose to survive the minimum\n"
        "chunk size, alongside the Go and TypeScript sources.\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return root


# -- chunking ---------------------------------------------------------------


def test_language_for_maps_known_suffixes(tmp_path):
    assert language_for(tmp_path / "a.go") == "go"
    assert language_for(tmp_path / "a.tsx") == "typescript"
    assert language_for(tmp_path / "a.PY") == "python"
    assert language_for(tmp_path / "a.png") is None


def test_chunk_text_splits_on_declaration_boundaries():
    source = (
        "package svc\n\n"
        + "func alpha() {\n" + "\t// work\n" * 12 + "}\n\n"
        + "func beta() {\n" + "\t// work\n" * 12 + "}\n"
    )
    chunks = chunk_text(source, path="svc.go", language="go")

    assert len(chunks) >= 1
    joined = "\n".join(chunk.text for chunk in chunks)
    assert "func alpha" in joined and "func beta" in joined


def test_chunk_text_keeps_leading_content_before_first_declaration():
    """The licence header and imports must not vanish when the first
    declaration appears well down the file."""
    source = "// Copyright ACME\n" + "// more header\n" * 10 + "\nfunc main() {\n\tx := 1\n}\n"
    chunks = chunk_text(source, path="main.go", language="go")

    assert any("Copyright ACME" in chunk.text for chunk in chunks)


def test_chunk_text_hard_splits_an_oversized_unit():
    giant = "func huge() {\n" + "\tdoSomethingUseful()\n" * 4000 + "}\n"
    chunks = chunk_text(giant, path="huge.go", language="go")

    assert len(chunks) > 1
    assert all(len(chunk.text) <= MAX_CHUNK_CHARS * 1.2 for chunk in chunks)


def test_chunk_text_drops_trivially_small_fragments():
    chunks = chunk_text("package x\n", path="x.go", language="go")
    assert all(len(chunk.text) >= MIN_CHUNK_CHARS for chunk in chunks)


def test_chunk_text_splits_markdown_on_headings():
    """Sections split once they exceed the budget; below it they pack together,
    which is why this fixture is deliberately oversized."""
    filler = "body text that is long enough to matter. " * 200
    source = f"# One\n\n{filler}\n\n## Two\n\n{filler}"
    chunks = chunk_text(source, path="doc.md", language="markdown")

    assert len(chunks) >= 2
    assert all(len(chunk.text) <= MAX_CHUNK_CHARS * 1.2 for chunk in chunks)


def test_chunk_text_packs_small_sections_together():
    """Two short sections are one chunk, not two — a heading alone is a poor
    retrieval unit."""
    source = "# One\n\n" + "short body. " * 10 + "\n\n## Two\n\n" + "short body. " * 10

    assert len(chunk_text(source, path="doc.md", language="markdown")) == 1


def test_chunk_text_ignores_empty_files():
    assert chunk_text("   \n\n", path="empty.go", language="go") == []


def test_chunk_line_numbers_are_one_indexed():
    chunks = chunk_text("x" * 200 + "\n", path="a.txt", language="text")
    assert chunks[0].start_line == 1


def test_embed_text_carries_the_file_path():
    """The path header is what makes a bare function body retrievable."""
    chunk = Chunk(path="internal/authz/rbac.go", language="go", start_line=10, end_line=20, text="func Can() {}")
    header = chunk.embed_text().splitlines()[0]

    assert "internal/authz/rbac.go" in header
    assert "10-20" in header


def test_chunk_file_skips_unindexable_and_undecodable(tmp_path):
    (tmp_path / "image.png").write_bytes(b"\x89PNG\r\n")
    (tmp_path / "binary.go").write_bytes(b"\xff\xfe\x00\x01not utf8")

    assert chunk_file(tmp_path / "image.png", repo_root=tmp_path) == []
    assert chunk_file(tmp_path / "binary.go", repo_root=tmp_path) == []


# -- embedder ---------------------------------------------------------------


def test_normalize_produces_unit_length():
    result = _normalize([3.0, 4.0])
    assert math.isclose(math.sqrt(sum(v * v for v in result)), 1.0)


def test_normalize_rejects_a_zero_vector():
    """A zero vector would silently rank identically against every query."""
    with pytest.raises(EmbeddingError, match="zero vector"):
        _normalize([0.0, 0.0])


def test_gemini_embedder_requires_a_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    with pytest.raises(EmbeddingError, match="GEMINI_API_KEY"):
        GeminiEmbedder().embed_query("anything")


def test_gemini_embedder_accepts_google_api_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")

    assert GeminiEmbedder()._resolve_api_key() == "test-key"


@pytest.mark.parametrize(
    "message,retryable",
    [
        ("429 rate limit exceeded", True),
        ("503 service unavailable", True),
        ("RESOURCE_EXHAUSTED", True),
        ("400 invalid argument", False),
        ("permission denied", False),
    ],
)
def test_retry_classification(message, retryable):
    """A bad request fails identically on retry — only waiting on a rate
    limit or a server fault is worth the latency."""
    assert _is_retryable(Exception(message)) is retryable


# -- store ------------------------------------------------------------------


def _chunks_for(path: str, count: int):
    return [
        Chunk(path=path, language="go", start_line=i * 10 + 1, end_line=i * 10 + 9, text=f"body {i}")
        for i in range(count)
    ]


def test_replace_file_round_trips(store):
    embedder = FakeEmbedder()
    chunks = _chunks_for("a.go", 2)
    vectors = embedder.embed_documents([c.text for c in chunks])

    store.replace_file("repo", "a.go", "hash1", chunks, vectors)

    assert store.file_hashes("repo") == {"a.go": "hash1"}
    assert store.repos() == [("repo", 1, 2)]


def test_replace_file_supersedes_previous_chunks(store):
    """Re-indexing an edited file must not leave the old chunks behind."""
    embedder = FakeEmbedder()
    first = _chunks_for("a.go", 3)
    store.replace_file("repo", "a.go", "h1", first, embedder.embed_documents([c.text for c in first]))
    second = _chunks_for("a.go", 1)
    store.replace_file("repo", "a.go", "h2", second, embedder.embed_documents([c.text for c in second]))

    assert store.repos() == [("repo", 1, 1)]
    assert store.file_hashes("repo") == {"a.go": "h2"}


def test_replace_file_rejects_mismatched_vector_count(store):
    with pytest.raises(VectorStoreError, match="2 chunks but 1 vectors"):
        store.replace_file("repo", "a.go", "h", _chunks_for("a.go", 2), [[0.0] * DIMS])


def test_forget_paths_removes_deleted_files(store):
    embedder = FakeEmbedder()
    chunks = _chunks_for("gone.go", 2)
    store.replace_file("repo", "gone.go", "h", chunks, embedder.embed_documents([c.text for c in chunks]))

    assert store.forget_paths("repo", ["gone.go"]) == 1
    assert store.file_hashes("repo") == {}


def test_search_ranks_by_similarity(store):
    embedder = FakeEmbedder()
    chunks = [
        Chunk(path="people.go", language="go", start_line=1, end_line=5,
              text="func CreateEmployee(name string) employee payroll staff"),
        Chunk(path="billing.go", language="go", start_line=1, end_line=5,
              text="func CalculateInvoice(total float64) invoice payment billing"),
    ]
    store.replace_file("repo", "people.go", "h1", chunks[:1], embedder.embed_documents([chunks[0].text]))
    store.replace_file("repo", "billing.go", "h2", chunks[1:], embedder.embed_documents([chunks[1].text]))

    hits = store.search("repo", embedder.embed_query("employee payroll staff"), limit=2)

    assert hits[0].path == "people.go"
    assert hits[0].score > hits[1].score


def test_search_respects_min_score(store):
    embedder = FakeEmbedder()
    chunks = _chunks_for("a.go", 1)
    store.replace_file("repo", "a.go", "h", chunks, embedder.embed_documents([c.text for c in chunks]))

    assert store.search("repo", embedder.embed_query("totally unrelated words"), min_score=0.99) == []


def test_search_on_an_unindexed_repo_explains_how_to_fix_it(store):
    with pytest.raises(VectorStoreError, match="index build"):
        store.search("never-indexed", [0.0] * DIMS)


def test_search_detects_a_dimension_change(store):
    """An index built with different settings would otherwise produce
    silently meaningless scores."""
    embedder = FakeEmbedder()
    chunks = _chunks_for("a.go", 1)
    store.replace_file("repo", "a.go", "h", chunks, embedder.embed_documents([c.text for c in chunks]))

    with pytest.raises(VectorStoreError, match="rebuild"):
        store.search("repo", [0.1] * (DIMS * 2))


def test_hit_location_is_clickable(store):
    embedder = FakeEmbedder()
    chunks = [Chunk(path="a/b.go", language="go", start_line=42, end_line=50, text="func Thing() {}")]
    store.replace_file("repo", "a/b.go", "h", chunks, embedder.embed_documents([chunks[0].text]))

    assert store.search("repo", embedder.embed_query("Thing"), limit=1)[0].location == "a/b.go:42"


# -- indexer ----------------------------------------------------------------


def test_discover_files_filters_to_source(repo):
    found = {path.name for path in discover_files(repo)}

    assert {"people.go", "billing.ts", "README.md"} <= found
    assert "logo.png" not in found


def test_discover_files_includes_uncommitted_work(repo):
    """A developer indexing mid-feature must see the file they are editing."""
    (repo / "draft.go").write_text("package draft\n\nfunc Draft() int {\n\treturn 1\n}\n", encoding="utf-8")

    assert "draft.go" in {path.name for path in discover_files(repo)}


def test_index_repository_embeds_and_is_searchable(repo, store):
    embedder = FakeEmbedder()

    result = index_repository(repo, embedder=embedder, store=store, repo_name="sample")

    assert result.files_embedded >= 3
    assert result.chunks_embedded >= 3
    assert not result.errors

    hits = search_repository("employee payroll", repo="sample", embedder=embedder, store=store, limit=1)
    assert hits[0].path == "internal/people.go"


def test_index_repository_is_incremental(repo, store):
    embedder = FakeEmbedder()
    index_repository(repo, embedder=embedder, store=store, repo_name="sample")
    calls_after_first = embedder.document_calls

    second = index_repository(repo, embedder=embedder, store=store, repo_name="sample")

    assert second.files_embedded == 0
    assert second.files_skipped_unchanged >= 3
    assert embedder.document_calls == calls_after_first  # nothing re-embedded


def test_index_repository_re_embeds_only_changed_files(repo, store):
    embedder = FakeEmbedder()
    index_repository(repo, embedder=embedder, store=store, repo_name="sample")
    (repo / "billing.ts").write_text(
        "export function calculateInvoiceTotal(items: number[]): number {\n"
        "  return items.reduce((a, b) => a + b, 1);\n}\n",
        encoding="utf-8",
    )

    second = index_repository(repo, embedder=embedder, store=store, repo_name="sample")

    assert second.files_embedded == 1


def test_index_repository_drops_deleted_files(repo, store):
    embedder = FakeEmbedder()
    index_repository(repo, embedder=embedder, store=store, repo_name="sample")
    (repo / "billing.ts").unlink()

    second = index_repository(repo, embedder=embedder, store=store, repo_name="sample")

    assert second.files_removed == 1
    assert "billing.ts" not in store.file_hashes("sample")


def test_index_repository_isolates_a_failing_file(repo, store):
    """One backend failure costs that file, not the run."""
    embedder = ExplodingEmbedder(fail_on="CreateEmployee")

    result = index_repository(repo, embedder=embedder, store=store, repo_name="sample")

    assert len(result.errors) == 1
    assert "people.go" in result.errors[0]
    assert result.files_embedded >= 1  # the rest still indexed


def test_index_repository_rebuild_clears_first(repo, store):
    embedder = FakeEmbedder()
    index_repository(repo, embedder=embedder, store=store, repo_name="sample")

    rebuilt = index_repository(repo, embedder=embedder, store=store, repo_name="sample", rebuild=True)

    assert rebuilt.files_embedded >= 3
    assert rebuilt.files_skipped_unchanged == 0


def test_index_repository_rejects_a_missing_directory(tmp_path, store):
    with pytest.raises(NotADirectoryError):
        index_repository(tmp_path / "nope", embedder=FakeEmbedder(), store=store)


def test_search_requires_a_query(store):
    with pytest.raises(ValueError, match="query is required"):
        search_repository("   ", repo="sample", embedder=FakeEmbedder(), store=store)


def test_content_hash_is_stable_and_sensitive():
    assert content_hash("abc") == content_hash("abc")
    assert content_hash("abc") != content_hash("abd")


def test_chunk_text_splits_a_single_oversized_line():
    """Minified bundles and generated SQL are one enormous line; line-based
    splitting alone would emit them over budget and lose the tail."""
    one_line = "SELECT * FROM t WHERE " + " OR ".join(f"id = {n}" for n in range(4000)) + ";"
    chunks = chunk_text(one_line, path="seed.sql", language="sql")

    assert len(chunks) > 1
    assert all(len(chunk.text) <= MAX_CHUNK_CHARS * 1.2 for chunk in chunks)
    # Nothing may be dropped on the floor.
    assert sum(len(chunk.text) for chunk in chunks) >= len(one_line) * 0.95


# -- module_relevance integration -------------------------------------------


def test_module_relevance_requires_context_or_repo():
    """The paste is no longer mandatory, but *some* description still is."""
    from meta_harness.module_relevance import analyze_module_relevance

    with pytest.raises(ValueError, match="module_context.*or repo"):
        analyze_module_relevance("T1", module_name="People")


def test_retrieve_module_context_cites_locations(repo, store, monkeypatch):
    """Retrieved spans must carry file:line so the verdict can be checked."""
    from meta_harness import module_relevance

    embedder = FakeEmbedder()
    index_repository(repo, embedder=embedder, store=store, repo_name="sample")
    monkeypatch.setattr(module_relevance, "VectorStore", lambda *a, **k: store)
    monkeypatch.setattr(module_relevance, "resolve_embedder", lambda **k: embedder)

    context = module_relevance.retrieve_module_context("employee payroll", repo="sample", limit=3)

    assert "internal/people.go:" in context
    assert "CreateEmployee" in context


def test_retrieve_module_context_reports_an_empty_index(store, monkeypatch):
    from meta_harness import module_relevance

    monkeypatch.setattr(module_relevance, "VectorStore", lambda *a, **k: store)
    monkeypatch.setattr(module_relevance, "resolve_embedder", lambda **k: FakeEmbedder())

    with pytest.raises(module_relevance.ModuleContextUnavailableError):
        module_relevance.retrieve_module_context("anything", repo="never-indexed")


def test_bulk_retrieves_module_context_once(repo, store, monkeypatch):
    """Retrieval is keyed on the module, so N tickets must not mean N queries —
    and the description must not drift between tickets in one sweep."""
    from meta_harness import module_relevance

    embedder = FakeEmbedder()
    index_repository(repo, embedder=embedder, store=store, repo_name="sample")
    monkeypatch.setattr(module_relevance, "VectorStore", lambda *a, **k: store)
    monkeypatch.setattr(module_relevance, "resolve_embedder", lambda **k: embedder)

    seen_contexts = []

    def fake_analyze(ticket_id, **kwargs):
        seen_contexts.append(kwargs["module_context"])
        return module_relevance.ModuleRelevance(
            ticket_id=ticket_id, ticket_name="t", module_name=kwargs["module_name"],
            verdict="related", confidence=0.9, rationale="ok",
            matched_aspects=[], module_gaps=[],
        )

    monkeypatch.setattr(module_relevance, "analyze_module_relevance", fake_analyze)
    queries_before = embedder.query_calls

    module_relevance.analyze_modules_bulk(
        ["T1", "T2", "T3"], module_name="employee payroll", repo="sample"
    )

    assert embedder.query_calls == queries_before + 1  # one retrieval, not three
    assert len(set(seen_contexts)) == 1  # every ticket judged against the same text


# -- backend selection ------------------------------------------------------


def test_default_backend_is_local(monkeypatch):
    """A fresh checkout must be able to index without anyone obtaining a key."""
    from meta_harness.embeddings.embedder import BACKEND_ENV_VAR, LocalEmbedder, resolve_embedder

    monkeypatch.delenv(BACKEND_ENV_VAR, raising=False)

    assert isinstance(resolve_embedder(), LocalEmbedder)


def test_backend_can_be_switched_to_gemini(monkeypatch):
    from meta_harness.embeddings.embedder import BACKEND_ENV_VAR, GeminiEmbedder, resolve_embedder

    monkeypatch.setenv(BACKEND_ENV_VAR, "gemini")

    assert isinstance(resolve_embedder(), GeminiEmbedder)


def test_unknown_backend_is_rejected(monkeypatch):
    from meta_harness.embeddings.embedder import BACKEND_ENV_VAR, resolve_embedder

    monkeypatch.setenv(BACKEND_ENV_VAR, "nonsense")

    with pytest.raises(EmbeddingError, match="Unknown embedding backend"):
        resolve_embedder()


def test_local_embedder_explains_a_missing_runtime(monkeypatch):
    """torch is an opt-in extra, so the error must name the install command
    rather than surfacing a bare ImportError."""
    import builtins

    from meta_harness.embeddings.embedder import LocalEmbedder

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name.startswith("sentence_transformers"):
            raise ImportError("no sentence_transformers")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    monkeypatch.setattr("meta_harness.embeddings.embedder._LOCAL_MODELS", {})

    with pytest.raises(EmbeddingError, match="local-embeddings"):
        LocalEmbedder()._model()


def test_local_embedder_truncates_and_renormalizes(monkeypatch):
    """Matryoshka truncation breaks unit length; leaving it broken would make
    every score subtly wrong."""
    from meta_harness.embeddings.embedder import LocalEmbedder

    class StubModel:
        def encode(self, texts, **kwargs):
            return [[0.5] * 2048 for _ in texts]

    embedder = LocalEmbedder(output_dimensions=256)
    monkeypatch.setattr(LocalEmbedder, "_model", lambda self: StubModel())

    vector = embedder.embed_query("anything")

    assert len(vector) == 256
    assert math.isclose(math.sqrt(sum(v * v for v in vector)), 1.0, rel_tol=1e-6)


def test_local_embedder_uses_the_right_prompt_per_side(monkeypatch):
    """Query and document must be embedded with different prompts — mixing
    them degrades ranking silently."""
    from meta_harness.embeddings.embedder import LocalEmbedder

    seen = []

    class StubModel:
        def encode(self, texts, **kwargs):
            seen.append(kwargs.get("prompt_name"))
            return [[1.0] + [0.0] * 2047 for _ in texts]

    embedder = LocalEmbedder()
    monkeypatch.setattr(LocalEmbedder, "_model", lambda self: StubModel())

    embedder.embed_documents(["a"])
    embedder.embed_query("b")

    assert seen == ["document", "query"]
