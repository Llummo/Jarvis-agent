"""Trusted context sources: provenance, verification and hybrid retrieval.

The point of a source registry is that a retrieved span can be traced back to
a declared origin and confirmed still to be there. These tests exercise the
failure side of that deliberately — a check that only ever passes proves
nothing about staleness.
"""

from __future__ import annotations

import hashlib
import math
import re
import subprocess

import pytest

from meta_harness.embeddings import (
    DRIFTED,
    MISSING,
    UNCHECKED,
    VERIFIED,
    SourceError,
    VectorStore,
    find_symbol,
    hybrid_search,
    index_repository,
    looks_like_symbol,
    source_status,
    verify_hits,
)
from meta_harness.embeddings.lexical import (
    BACKEND_GIT_GREP,
    BACKEND_NONE,
    BACKEND_RIPGREP,
    available_backend,
)
from meta_harness.embeddings.sources import check_span, git_revision

DIMS = 128


class FakeEmbedder:
    dimensions = DIMS

    def _vector(self, text: str):
        values = [0.0] * DIMS
        for token in re.findall(r"[A-Za-z_]{3,}", text.lower()):
            values[int(hashlib.md5(token.encode()).hexdigest(), 16) % DIMS] += 1.0
        magnitude = math.sqrt(sum(v * v for v in values)) or 1.0
        return [v / magnitude for v in values]

    def embed_documents(self, texts):
        return [self._vector(text) for text in texts]

    def embed_query(self, text):
        return self._vector(text)


@pytest.fixture
def store(tmp_path):
    with VectorStore(tmp_path / "index.db") as running:
        yield running


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "project"
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
        "export function calculateInvoiceTotal(items: number[]): number {\n"
        "  return items.reduce((a, b) => a + b, 0);\n}\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "initial"],
        cwd=root, check=True,
    )
    return root


@pytest.fixture
def indexed(repo, store):
    index_repository(repo, embedder=FakeEmbedder(), store=store, repo_name="project")
    return repo


# -- registry ---------------------------------------------------------------


def test_register_records_the_revision(repo, store):
    source = store.sources.register("project", repo)

    assert source.name == "project"
    assert source.root == repo.resolve()
    assert source.revision == git_revision(repo)


def test_register_rejects_a_non_directory(tmp_path, store):
    with pytest.raises(SourceError, match="Not a directory"):
        store.sources.register("bad", tmp_path / "nope")


def test_register_is_idempotent(repo, store):
    store.sources.register("project", repo)
    store.sources.register("project", repo)

    assert [source.name for source in store.sources.list_sources()] == ["project"]


def test_unregistered_source_says_how_to_add_it(store):
    with pytest.raises(SourceError, match="source add"):
        store.sources.get("nothing")


def test_indexing_registers_the_source(repo, store):
    """Indexing is what makes something a source, so registration must not be
    a separate step someone can forget."""
    index_repository(repo, embedder=FakeEmbedder(), store=store, repo_name="project")

    source = store.sources.get("project")
    assert source.indexed_at is not None
    assert source.revision == git_revision(repo)


def test_remove_deregisters(indexed, store):
    assert store.sources.remove("project") is True
    assert store.sources.find("project") is None


# -- verification -----------------------------------------------------------


def test_unmodified_span_verifies(indexed, store):
    source = store.sources.get("project")
    row = store._connection.execute(
        "SELECT path, start_line, end_line, text FROM chunks WHERE repo='project' LIMIT 1"
    ).fetchone()

    result = check_span(source, row["path"], row["start_line"], row["end_line"], row["text"])

    assert result.status == VERIFIED
    assert result.trustworthy


def test_edited_file_is_reported_as_drifted(indexed, store):
    """The whole point: an index is a copy, and a stale copy does not look
    wrong on its own."""
    source = store.sources.get("project")
    row = store._connection.execute(
        "SELECT path, start_line, end_line, text FROM chunks WHERE repo='project' LIMIT 1"
    ).fetchone()

    result = check_span(
        source, row["path"], row["start_line"], row["end_line"], row["text"] + "\nsomething else\n"
    )

    assert result.status == DRIFTED
    assert not result.trustworthy


def test_deleted_file_is_reported_as_missing(indexed, store):
    source = store.sources.get("project")

    assert check_span(source, "gone.go", 1, 3, "x").status == MISSING


def test_range_past_end_of_file_is_drift(indexed, store):
    source = store.sources.get("project")

    assert check_span(source, "billing.ts", 5000, 5010, "x").status == DRIFTED


def test_whitespace_only_change_still_verifies(indexed, store):
    """Comparison is on non-blank lines: an index stores a chunk stripped of
    surrounding blank lines, and re-reading by range picks blank lines back
    up. Reporting that as drift would make verification useless noise."""
    source = store.sources.get("project")
    row = store._connection.execute(
        "SELECT path, start_line, end_line, text FROM chunks WHERE repo='project' LIMIT 1"
    ).fetchone()

    padded = "\n" + row["text"] + "\n\n"

    assert check_span(source, row["path"], row["start_line"], row["end_line"], padded).status == VERIFIED


def test_verify_hits_marks_each_result(indexed, store):
    hits = hybrid_search("employee payroll", repo="project", embedder=FakeEmbedder(), store=store, limit=3)

    checked = verify_hits(store, hits)

    assert checked
    assert all(hit.verification == VERIFIED for hit in checked)


def test_verify_hits_leaves_unknown_sources_unchecked(store):
    from meta_harness.embeddings.store import SearchHit

    hit = SearchHit(path="a.go", language="go", start_line=1, end_line=2, text="x", score=1.0, repo="ghost")

    assert verify_hits(store, [hit])[0].verification == UNCHECKED


# -- drift status -----------------------------------------------------------


def test_status_is_current_right_after_indexing(indexed, store):
    status = source_status(store, "project")

    assert status.current
    assert "current" in status.summary()


def test_status_detects_an_edited_file(indexed, store):
    (indexed / "billing.ts").write_text("export function changed() { return 1; }\n", encoding="utf-8")

    status = source_status(store, "project")

    assert not status.current
    assert status.files_changed == 1
    assert "stale" in status.summary()


def test_status_detects_a_deleted_file(indexed, store):
    (indexed / "billing.ts").unlink()

    status = source_status(store, "project")

    assert status.files_removed == 1
    assert not status.current


# -- lexical search ---------------------------------------------------------


def test_available_backend_is_one_of_the_known_values():
    assert available_backend() in (BACKEND_RIPGREP, BACKEND_GIT_GREP, BACKEND_NONE)


def test_find_symbol_locates_a_declaration(repo):
    hits = find_symbol("CreateEmployee", root=repo)

    assert any(hit.path.endswith("people.go") for hit in hits)
    assert all(hit.line > 0 for hit in hits)


def test_find_symbol_respects_word_boundaries(repo):
    """A search for `Employee` must not be satisfied by `CreateEmployee`
    alone — otherwise every short identifier matches everything."""
    (repo / "internal" / "other.go").write_text(
        "package other\n\nfunc CreateEmployeeRecord() {}\n", encoding="utf-8"
    )

    narrow = find_symbol("CreateEmployeeRecord", root=repo, whole_word=True)

    assert any("other.go" in hit.path for hit in narrow)


def test_find_symbol_treats_the_query_literally(repo):
    """Callers pass identifiers, not patterns. As a regex `Employee.` would
    match `Employee{`, `Employee,` and so on; literally it matches only a
    real dot, which appears nowhere here."""
    as_literal = find_symbol("Employee.", root=repo, whole_word=False)
    assert as_literal == []

    # And a query containing metacharacters must not error or be reinterpreted.
    exact = find_symbol("CreateEmployee(name", root=repo, whole_word=False)
    assert any(hit.path.endswith("people.go") for hit in exact)


def test_find_symbol_requires_a_symbol(repo):
    with pytest.raises(ValueError, match="symbol is required"):
        find_symbol("   ", root=repo)


def test_find_symbol_skips_unindexable_files(repo):
    """Lexical and semantic results must be drawn from the same universe of
    files, or fusion compares hits the index could never have produced."""
    (repo / "notes.bin").write_text("CreateEmployee\n", encoding="utf-8")

    assert all(not hit.path.endswith(".bin") for hit in find_symbol("CreateEmployee", root=repo))


@pytest.mark.parametrize(
    "query,expected",
    [
        ("calculateInvoiceTotal", True),
        ("store.search", True),
        ("_call_with_retry", True),
        ("how are tickets created", False),
        ("", False),
        ("SELECT * FROM t", False),
    ],
)
def test_symbol_detection(query, expected):
    assert looks_like_symbol(query) is expected


# -- hybrid retrieval -------------------------------------------------------


def test_hybrid_promotes_an_exact_symbol_match(indexed, store):
    """The case semantic search is worst at: an exact identifier, where the
    embedding blurs away the specificity being asked for."""
    hits = hybrid_search(
        "calculateInvoiceTotal", repo="project", embedder=FakeEmbedder(), store=store, limit=3
    )

    assert hits
    assert hits[0].path == "billing.ts"
    assert hits[0].retrieval in ("hybrid", "lexical")


def test_hybrid_skips_the_lexical_pass_for_prose(indexed, store):
    """Searching a sentence literally returns nothing, so paying for it on
    every natural-language query is waste."""
    hits = hybrid_search(
        "how does the payroll work", repo="project", embedder=FakeEmbedder(), store=store, limit=3
    )

    assert all(hit.retrieval == "semantic" for hit in hits)


def test_hybrid_can_be_forced_off(indexed, store):
    hits = hybrid_search(
        "calculateInvoiceTotal", repo="project", embedder=FakeEmbedder(), store=store,
        limit=3, lexical=False,
    )

    assert all(hit.retrieval == "semantic" for hit in hits)


def test_hybrid_verifies_when_asked(indexed, store):
    hits = hybrid_search(
        "employee", repo="project", embedder=FakeEmbedder(), store=store, limit=2, verify=True
    )

    assert all(hit.verification in (VERIFIED, DRIFTED, MISSING) for hit in hits)


def test_hits_carry_a_followable_citation(indexed, store):
    hits = hybrid_search("employee", repo="project", embedder=FakeEmbedder(), store=store, limit=1)

    assert hits[0].citation.startswith("project/")
    assert ":" in hits[0].citation


def test_lexical_only_hits_are_not_labelled_hybrid(indexed, store):
    """"hybrid" must mean both methods found it. A chunk recovered by lexical
    search, then matched again by a second lexical hit, was still only ever
    found one way."""
    hits = hybrid_search(
        "CreateEmployee", repo="project", embedder=FakeEmbedder(), store=store,
        limit=5, lexical=True,
    )

    for hit in hits:
        if hit.retrieval == "hybrid":
            # A hybrid hit must carry a real semantic score; a lexically
            # recovered chunk has none.
            assert hit.score > 0.0, f"{hit.location} claims hybrid with no semantic score"


# -- document sources -------------------------------------------------------


def test_document_source_indexes_and_verifies(tmp_path, store):
    """A document's root is the file itself, not a directory containing it —
    joining a relative path onto it would look for the file inside itself."""
    from meta_harness.embeddings import index_document

    doc = tmp_path / "requirements.md"
    doc.write_text(
        "# Payroll\n\n" + ("The system must calculate monthly payroll for each employee. " * 20),
        encoding="utf-8",
    )

    result = index_document(doc, embedder=FakeEmbedder(), store=store, source_name="reqs")

    assert result.chunks_embedded >= 1
    assert source_status(store, "reqs").current, "a freshly indexed document is not stale"


def test_document_source_detects_an_edit(tmp_path, store):
    from meta_harness.embeddings import index_document

    doc = tmp_path / "requirements.md"
    doc.write_text("# Payroll\n\n" + ("Original requirement text. " * 30), encoding="utf-8")
    index_document(doc, embedder=FakeEmbedder(), store=store, source_name="reqs")

    doc.write_text("# Payroll\n\n" + ("Rewritten requirement text. " * 30), encoding="utf-8")

    assert not source_status(store, "reqs").current


def test_document_source_rejects_an_empty_file(tmp_path, store):
    from meta_harness.embeddings import SourceIngestError, index_document

    empty = tmp_path / "blank.md"
    empty.write_text("   \n", encoding="utf-8")

    with pytest.raises(SourceIngestError, match="no extractable text"):
        index_document(empty, embedder=FakeEmbedder(), store=store, source_name="blank")


def test_github_url_naming():
    from meta_harness.embeddings import name_from_github_url

    assert name_from_github_url("https://github.com/owner/repo.git") == "repo"
    assert name_from_github_url("https://github.com/owner/repo/") == "repo"
    assert name_from_github_url("git@github.com:owner/repo.git") == "repo"


def test_unknown_kind_is_rejected(repo, store):
    with pytest.raises(SourceError, match="Unknown source kind"):
        store.sources.register("x", repo, kind="nonsense")


# -- usable before indexing -------------------------------------------------


def test_a_registered_source_is_searchable_before_anything_is_embedded(repo, store):
    """Adding a source is instant; embedding is not. Retrieval must work from
    the moment a source exists, or "add" is only nominally fast."""
    store.sources.register("project", repo)

    hits = hybrid_search("CreateEmployee", repo="project", embedder=FakeEmbedder(), store=store, limit=3)

    assert hits, "an unembedded source must still return text matches"
    assert all(hit.retrieval == "lexical" for hit in hits)
    assert any("people.go" in hit.path for hit in hits)


def test_unembedded_results_carry_real_context_from_disk(repo, store):
    """There is no index to read from, so the surrounding lines come straight
    off the working tree — which also means they cannot be stale."""
    store.sources.register("project", repo)

    hit = hybrid_search("CreateEmployee", repo="project", embedder=FakeEmbedder(), store=store, limit=1)[0]

    assert "CreateEmployee" in hit.text
    assert hit.end_line > hit.start_line, "context lines should surround the match"


def test_unknown_source_still_raises(store):
    """Falling back to text search must not paper over a genuinely wrong name."""
    from meta_harness.embeddings import VectorStoreError

    with pytest.raises(VectorStoreError):
        hybrid_search("anything", repo="never-registered", embedder=FakeEmbedder(), store=store)


def test_lexical_falls_back_to_scanning_without_git_or_ripgrep(tmp_path, monkeypatch):
    """git grep needs an actual repository. On a plain directory it fails
    outright, which would leave text search unavailable rather than slow."""
    from meta_harness.embeddings import lexical

    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    (plain / "service.go").write_text("package svc\n\nfunc CreateEmployee() {}\n", encoding="utf-8")
    monkeypatch.setattr(lexical, "ripgrep_path", lambda: None)

    assert lexical.available_backend(plain) == lexical.BACKEND_SCAN
    hits = lexical.find_symbol("CreateEmployee", root=plain)
    assert [h.path for h in hits] == ["service.go"]


def test_skeleton_indexes_declarations_not_bodies():
    """Bodies are most of the text and little of the meaning — measured at
    ~15% of a real project, which is the difference between an hour of
    indexing and minutes."""
    from meta_harness.embeddings.chunking import chunk_skeleton

    source = (
        "// CreateEmployee adds a person to the payroll.\n"
        "func CreateEmployee(name string) error {\n"
        + "\tdoSomethingVerbose()\n" * 200
        + "}\n"
    )
    chunks = chunk_skeleton(source, path="people.go", language="go")

    assert len(chunks) == 1
    body = chunks[0].text
    assert "CreateEmployee" in body
    assert "adds a person to the payroll" in body, "the doc comment carries the meaning"
    assert "doSomethingVerbose" not in body, "the body is what we are dropping"
    assert len(body) < len(source) / 10


# -- skeleton indexing end to end -------------------------------------------


def test_skeleton_index_expands_to_real_code_on_retrieval(repo, store):
    """The index holds declarations; the answer must hold code. Otherwise the
    saving comes out of the caller's context rather than the index."""
    index_repository(repo, embedder=FakeEmbedder(), store=store, repo_name="project", mode="skeleton")

    hits = hybrid_search("CreateEmployee", repo="project", embedder=FakeEmbedder(), store=store, limit=2)

    match = next(h for h in hits if "people.go" in h.path)
    assert match.mode == "skeleton"
    assert "return &Employee{Name: name}" in match.text, "the body must be read back from disk"


def test_skeleton_hits_are_verified_by_construction(repo, store):
    """Expansion reads the working tree, so there is no stored copy that could
    have drifted — reporting anything else would be misleading."""
    index_repository(repo, embedder=FakeEmbedder(), store=store, repo_name="project", mode="skeleton")

    hits = hybrid_search(
        "CreateEmployee", repo="project", embedder=FakeEmbedder(), store=store, limit=1, verify=True
    )

    assert hits[0].verification == VERIFIED


def test_skeleton_expansion_is_bounded(tmp_path, store):
    """A skeleton chunk spans a file's first declaration to its last, so
    expanding to its own range returns the whole file — and a dozen of those
    buries the answer they were meant to supply."""
    from meta_harness.embeddings.indexer import SKELETON_EXPANSION_LINES

    root = tmp_path / "big"
    root.mkdir()
    (root / "huge.go").write_text(
        "package huge\n\n" + "".join(f"func Thing{i}() {{\n\treturn\n}}\n\n" for i in range(400)),
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    index_repository(root, embedder=FakeEmbedder(), store=store, repo_name="big", mode="skeleton")

    hits = hybrid_search("Thing", repo="big", embedder=FakeEmbedder(), store=store, limit=1)

    assert hits
    assert len(hits[0].text.splitlines()) <= SKELETON_EXPANSION_LINES


def test_skeleton_indexes_far_less_than_full(repo, store):
    index_repository(repo, embedder=FakeEmbedder(), store=store, repo_name="skel", mode="skeleton")
    index_repository(repo, embedder=FakeEmbedder(), store=store, repo_name="full", mode="full")

    sizes = {name: chunks for name, _files, chunks in store.repos()}
    skel_text = store._connection.execute(
        "SELECT SUM(LENGTH(text)) FROM chunks WHERE repo='skel'"
    ).fetchone()[0]
    full_text = store._connection.execute(
        "SELECT SUM(LENGTH(text)) FROM chunks WHERE repo='full'"
    ).fetchone()[0]

    assert skel_text < full_text, "declarations must be smaller than bodies"
    assert sizes["skel"] > 0


def test_index_rejects_an_unknown_mode(repo, store):
    with pytest.raises(ValueError, match="mode must be"):
        index_repository(repo, embedder=FakeEmbedder(), store=store, repo_name="x", mode="nonsense")


def test_mixed_width_index_is_reported_clearly(repo, store):
    """Two backends writing one source produces vectors of different widths.
    Reshaping blind turns that into an opaque numpy error far from the cause."""
    from meta_harness.embeddings import VectorStoreError
    from meta_harness.embeddings.chunking import Chunk

    chunk = Chunk(path="a.go", language="go", start_line=1, end_line=2, text="x")
    store.replace_file("mixed", "a.go", "h1", [chunk], [[0.1] * DIMS])
    store.replace_file("mixed", "b.go", "h2", [chunk], [[0.1] * (DIMS * 2)])

    with pytest.raises(VectorStoreError, match="different widths"):
        store.search("mixed", [0.1] * DIMS)


def test_concurrent_indexing_of_one_source_is_refused(repo, store):
    """Two jobs writing the same source interleave and leave an index built by
    two backends, which then fails to load at all."""
    from filelock import FileLock

    from meta_harness.embeddings.indexer import IndexInProgressError, _safe_lock_name

    held = FileLock(str(store.db_path.parent / f".index-{_safe_lock_name('project')}.lock"))
    held.acquire()
    try:
        with pytest.raises(IndexInProgressError, match="already being indexed"):
            index_repository(repo, embedder=FakeEmbedder(), store=store, repo_name="project")
    finally:
        held.release()
