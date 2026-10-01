"""Tests for ``scripts/evaluate_chunk_context.py`` — the chunk-context experiment.

Pure logic on invented documents and vectors: no database, no network. The
key property is that the replay reproduces production chunking exactly, so
the experiment's contextual inputs describe the real stored chunks.
"""

import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPOSITORY_ROOT / "scripts"))

import evaluate_chunk_context as cc
import evaluate_embeddings as ev
from fake_embedder import FakeEmbedder

from preston.canonical import (
    Block,
    CanonicalDocument,
    FaqPair,
    Heading,
    ImageRef,
    ListBlock,
    Paragraph,
    Provenance,
    hash_content,
)
from preston.embedding import Embedder
from preston.ingestion import CHUNK_OVERLAP, CHUNK_SIZE, build_chunks


def document(*blocks: Block, title: str = "Doc") -> CanonicalDocument:
    return CanonicalDocument(
        canonical_uri="https://example.test/doc",
        source_type="mysql",
        content_type="blog",
        source_scope="blog",
        title=title,
        blocks=blocks,
        provenance=Provenance(
            retrieved_at=datetime(2026, 10, 1, tzinfo=UTC),
            extractor_version=1,
            normalizer_version=1,
        ),
    )


LONG = "word " * 300  # 1,500 characters: spans several windows


def test_the_replay_reproduces_production_chunking() -> None:
    doc = document(
        Heading(level=1, text="Top"),
        Paragraph(text=LONG),
        Heading(level=2, text="Section A"),
        ListBlock(items=("one", "two"), ordered=False),
        FaqPair(question="Q?", answer=(Paragraph(text="A."),)),
        Heading(level=2, text="Section B"),
        Paragraph(text=LONG),
        ImageRef(src="/x.png", alt="Contact us"),
        Paragraph(text="Tail."),
    )

    assert [c.content for c in cc.contextual_chunks(doc)] == build_chunks(doc)


def test_a_heading_closes_deeper_sections_and_replaces_its_own_level() -> None:
    doc = document(
        Heading(level=1, text="H1"),
        Heading(level=2, text="A"),
        Heading(level=3, text="A.1"),
        Paragraph(text="under A.1"),
    )
    later = document(
        Heading(level=1, text="H1"),
        Heading(level=2, text="A"),
        Heading(level=3, text="A.1"),
        FaqPair(question="split", answer=(Paragraph(text="x"),)),
        Heading(level=2, text="B"),
        Paragraph(text="under B"),
    )

    # One window, starting at the "H1" heading: only "H1" is in force there.
    assert cc.contextual_chunks(doc)[0].heading_path == ("H1",)
    assert cc.contextual_chunks(later)[-1].heading_path == ("H1", "B")


def test_a_window_takes_the_path_in_force_at_its_first_character() -> None:
    doc = document(
        Heading(level=2, text="First"),
        Paragraph(text=LONG),
        Heading(level=2, text="Second"),
        Paragraph(text=LONG),
    )
    chunks = cc.contextual_chunks(doc)
    step = CHUNK_SIZE - CHUNK_OVERLAP
    joined = f"First\n\n{LONG}\n\nSecond\n\n{LONG}"  # the run as the chunker joins it
    second_starts_at = joined.index("Second")

    for index, chunk in enumerate(chunks):
        expected = ("First",) if index * step < second_starts_at else ("Second",)
        assert chunk.heading_path == expected, index
    assert {c.heading_path for c in chunks} == {("First",), ("Second",)}


def test_an_faq_pair_takes_the_path_where_it_sits() -> None:
    doc = document(
        Heading(level=2, text="Questions"),
        FaqPair(question="Is it?", answer=(Paragraph(text="Yes."),)),
    )

    faq = cc.contextual_chunks(doc)[-1]
    assert faq.content == "Is it?\nYes."
    assert faq.heading_path == ("Questions",)


def test_content_before_any_heading_has_an_empty_path() -> None:
    assert cc.contextual_chunks(document(Paragraph(text="intro")))[0].heading_path == ()


def test_the_contextual_text_is_title_path_then_the_unchanged_chunk() -> None:
    chunk = cc.ContextualChunk("Body text.", ("Doc", "Section", "Sub"))

    text = cc.contextual_text("Doc", chunk)

    # The heading that repeats the title is dropped; the chunk is untouched.
    assert text == "Doc › Section › Sub\n\nBody text."
    assert text.endswith(chunk.content)
    assert cc.contextual_text("Doc", cc.ContextualChunk("Body", ())) == "Doc\n\nBody"


# ---------------------------------------------------------------------------
# Baseline artifact reading
# ---------------------------------------------------------------------------


def baseline_artifact(
    directory: Path, vectors: dict[str, list[float]], dims: int
) -> Path:
    store = ev.VectorStore(directory, model=cc.MODEL, dimensions=dims)
    store.append(list(vectors), list(vectors.values()))
    return directory


def test_the_baseline_artifact_is_read_without_being_modified(tmp_path: Path) -> None:
    directory = baseline_artifact(tmp_path / "base", {"a": [1.0, 2.0]}, 2)
    before = {p.name: p.read_bytes() for p in directory.iterdir()}

    vectors = cc.read_artifact(directory, 2)

    assert list(vectors["a"]) == [1.0, 2.0]
    assert {p.name: p.read_bytes() for p in directory.iterdir()} == before


def test_a_baseline_of_the_wrong_dimension_or_size_is_refused(tmp_path: Path) -> None:
    directory = baseline_artifact(tmp_path / "base", {"a": [1.0, 2.0]}, 2)

    with pytest.raises(ValueError, match="not a"):
        _ = cc.read_artifact(directory, 4)
    (directory / "vectors.f32").write_bytes(b"\x00" * 4)
    with pytest.raises(ValueError, match="truncated"):
        _ = cc.read_artifact(directory, 2)


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------

DIMS = cc.DIMENSIONS
CHUNKS = (
    ev.Chunk(
        hash_content("alpha body"), "https://example.test/a", "grc", 0, "alpha body"
    ),
    ev.Chunk(
        hash_content("beta body"), "https://example.test/b", "blog", 0, "beta body"
    ),
)


def write_golden(path: Path) -> Path:
    cases = [
        {
            "id": "q1",
            "category": "faq",
            "query": "find alpha",
            "answerable": True,
            "expected_document_uris": ["https://example.test/a"],
            "expected_chunk_hashes": [CHUNKS[0].content_hash],
            "expected_source_scopes": ["grc"],
            "notes": "n",
        },
        {
            "id": "q2",
            "category": "unanswerable",
            "query": "nothing here",
            "answerable": False,
            "expected_document_uris": [],
            "expected_chunk_hashes": [],
            "expected_source_scopes": [],
            "absent_terms": ["zz"],
            "notes": "n",
        },
    ]
    path.write_text(
        json.dumps({"version": 2, "built_against": {"chunks": 2}, "cases": cases}),
        "utf-8",
    )
    return path


def run(tmp_path: Path, *flags: str, embedder: Embedder | None = None) -> int:
    fake = FakeEmbedder(dimensions=DIMS)
    base = baseline_artifact(
        tmp_path / "base",
        {c.content_hash: fake.vector_for(c.content) for c in CHUNKS}
        | {hash_content("find alpha"): fake.vector_for("find alpha")},
        DIMS,
    )
    corpus = cc.Corpus(
        chunks=CHUNKS,
        contextual={c.content_hash: f"Title\n\n{c.content}" for c in CHUNKS},
        production_embeddings=0,
    )

    def must_not_be_called() -> Embedder:
        pytest.fail("the embedder factory was called")

    return cc.main(
        [
            "--dataset",
            str(write_golden(tmp_path / "golden.json")),
            "--artifact-dir",
            str(tmp_path / "context"),
            *flags,
        ],
        corpus_loader=lambda: corpus,
        embedder_factory=(lambda: embedder) if embedder else must_not_be_called,
        baseline_dir=base,
    )


def test_without_the_api_flag_no_embedder_is_built(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status = run(tmp_path)

    out = capsys.readouterr().out
    assert status == 0
    assert "queries reused from baseline: 1; new: 1" in out
    assert "Still to embed: 3 texts" in out  # 2 contextual chunks + 1 new query
    assert "No API call made" in out


def test_a_full_run_embeds_only_what_the_baseline_lacks(tmp_path: Path) -> None:
    fake = FakeEmbedder(dimensions=DIMS)

    status = run(tmp_path, "--run-api", embedder=fake)

    assert status == 0
    embedded = sorted(text for batch in fake.calls for text in batch)
    assert embedded == ["Title\n\nalpha body", "Title\n\nbeta body", "nothing here"]
    report = json.loads((tmp_path / "context" / "report.json").read_text("utf-8"))
    assert report["evaluation_only"] is True
    assert {case["case_id"] for case in report["cases"]} == {"q1", "q2"}
    assert "all/current" in report["metrics"] and "all/contextual" in report["metrics"]


def test_identical_representations_score_identically() -> None:
    """With the same vectors for both, the comparison reports no change."""
    fake = FakeEmbedder(dimensions=DIMS)
    golden = ev.Golden(
        cases=(
            ev.GoldenCase(
                "q1",
                "faq",
                "alpha body",
                True,
                frozenset({"https://example.test/a"}),
                frozenset({CHUNKS[0].content_hash}),
            ),
        ),
        expected_chunks=2,
        sha256="0" * 64,
    )
    vectors: dict[str, Sequence[float]] = {
        c.content_hash: fake.vector_for(c.content) for c in CHUNKS
    }
    queries = {hash_content("alpha body"): fake.vector_for("alpha body")}

    a = cc.score(golden, CHUNKS, vectors, queries)
    b = cc.score(golden, CHUNKS, vectors, queries)

    assert [r.any_of_rank for r in a] == [r.any_of_rank for r in b] == [1]
