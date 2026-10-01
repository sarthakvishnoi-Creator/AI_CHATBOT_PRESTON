"""Tests for ``scripts/evaluate_embeddings.py``.

Everything runs on invented vectors and a scripted embedder: no OpenAI call
and no database. The command-line tests inject the corpus and the embedder,
so "no ``--run-api`` means no network call" is checked by an embedder
factory that fails the test if it is ever invoked.
"""

import json
import math
import sys
from array import array
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from openai import AsyncOpenAI
from openai.types import CreateEmbeddingResponse, Embedding
from openai.types.create_embedding_response import Usage

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPOSITORY_ROOT / "scripts"))

import evaluate_embeddings as ev
from fake_embedder import FakeEmbedder

from preston.canonical import hash_content
from preston.embedding import Embedder, EmbeddingError, OpenAIEmbedder

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def chunk(text: str, uri: str, index: int = 0, scope: str = "grc") -> ev.Chunk:
    return ev.Chunk(
        content_hash=hash_content(text),
        document_uri=uri,
        source_scope=scope,
        chunk_index=index,
        content=text,
    )


def case(
    case_id: str,
    *,
    documents: Sequence[str] = (),
    chunks: Sequence[ev.Chunk] = (),
    answerable: bool = True,
    category: str = "paraphrase",
) -> ev.GoldenCase:
    return ev.GoldenCase(
        id=case_id,
        category=category,
        query=f"query for {case_id}",
        answerable=answerable,
        expected_document_uris=frozenset(documents),
        expected_chunk_hashes=frozenset(c.content_hash for c in chunks),
    )


def ranked(*pairs: tuple[ev.Chunk, float]) -> list[tuple[ev.Chunk, float]]:
    return list(pairs)


def sparse(values: dict[int, float], dimensions: int = ev.DIMENSIONS) -> list[float]:
    vector = [0.0] * dimensions
    for index, value in values.items():
        vector[index] = value
    return vector


class ScriptedEmbedder:
    """Returns a chosen sparse vector per text and records every call."""

    def __init__(
        self, table: dict[str, dict[int, float]], dimensions: int = ev.DIMENSIONS
    ) -> None:
        self.table = table
        self.dimensions = dimensions
        self.calls: list[list[str]] = []

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [sparse(self.table[text], self.dimensions) for text in texts]


# ---------------------------------------------------------------------------
# Cosine similarity
# ---------------------------------------------------------------------------


def test_cosine_of_identical_vectors_is_one() -> None:
    assert ev.cosine_similarity([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(1.0)


def test_cosine_of_orthogonal_vectors_is_zero() -> None:
    assert ev.cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_of_opposite_vectors_is_minus_one() -> None:
    assert ev.cosine_similarity([1.0, 2.0], [-1.0, -2.0]) == pytest.approx(-1.0)


def test_cosine_ignores_vector_length() -> None:
    assert ev.cosine_similarity([1.0, 1.0], [5.0, 5.0]) == pytest.approx(1.0)


def test_cosine_matches_a_hand_computed_value() -> None:
    # (1*2 + 2*0 + 3*1) / (sqrt(14) * sqrt(5)) = 5 / sqrt(70)
    assert ev.cosine_similarity([1.0, 2.0, 3.0], [2.0, 0.0, 1.0]) == pytest.approx(
        5 / math.sqrt(70)
    )


def test_cosine_rejects_vectors_of_different_length() -> None:
    with pytest.raises(ValueError, match="differ in length"):
        _ = ev.cosine_similarity([1.0, 2.0], [1.0, 2.0, 3.0])


def test_cosine_rejects_a_zero_vector() -> None:
    with pytest.raises(ValueError, match="zero vector"):
        _ = ev.cosine_similarity([0.0, 0.0], [1.0, 1.0])


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


def test_ranking_orders_chunks_by_descending_similarity() -> None:
    near, middle, far = chunk("near", "u1"), chunk("middle", "u2"), chunk("far", "u3")
    vectors = {
        near.content_hash: [1.0, 0.1],
        middle.content_hash: [1.0, 1.0],
        far.content_hash: [0.0, 1.0],
    }

    order = [
        c.content for c, _ in ev.rank_chunks([1.0, 0.0], [far, middle, near], vectors)
    ]

    assert order == ["near", "middle", "far"]


def test_ranking_agrees_with_cosine_similarity() -> None:
    chunks = [chunk(f"text {n}", f"u{n}") for n in range(6)]
    vectors = {
        c.content_hash: [float(n + 1), float(6 - n), 0.5] for n, c in enumerate(chunks)
    }
    query = [0.3, 1.0, 0.2]

    scored = ev.rank_chunks(query, chunks, vectors)

    for found, similarity in scored:
        assert similarity == pytest.approx(
            ev.cosine_similarity(query, vectors[found.content_hash])
        )


def test_ranking_is_deterministic_whatever_the_input_order() -> None:
    chunks = [chunk(f"tie {n}", f"u{n}") for n in range(5)]
    vectors = {c.content_hash: [1.0, 0.0] for c in chunks}  # every chunk ties

    forward = ev.rank_chunks([1.0, 0.0], chunks, vectors)
    backward = ev.rank_chunks([1.0, 0.0], list(reversed(chunks)), vectors)

    assert [c.content_hash for c, _ in forward] == [c.content_hash for c, _ in backward]
    assert [c.content_hash for c, _ in forward] == sorted(
        c.content_hash for c in chunks
    )


def test_ranking_rejects_a_vector_of_the_wrong_length() -> None:
    bad = chunk("bad", "u")

    with pytest.raises(ValueError, match="differ in length"):
        _ = ev.rank_chunks([1.0, 0.0], [bad], {bad.content_hash: [1.0, 0.0, 0.0]})


def test_ranking_rejects_a_zero_query() -> None:
    only = chunk("only", "u")

    with pytest.raises(ValueError, match="zero vector"):
        _ = ev.rank_chunks([0.0, 0.0], [only], {only.content_hash: [1.0, 0.0]})


def test_ranking_rejects_a_zero_chunk_vector() -> None:
    only = chunk("only", "u")

    with pytest.raises(ValueError, match="zero vector"):
        _ = ev.rank_chunks([1.0, 0.0], [only], {only.content_hash: [0.0, 0.0]})


# ---------------------------------------------------------------------------
# Any-of evidence
# ---------------------------------------------------------------------------

EXPECTED_DOC = "https://example.test/expected"
OTHER_DOC = "https://example.test/other"


def test_an_expected_chunk_in_the_expected_document_is_a_hit_at_its_rank() -> None:
    target = chunk("target", EXPECTED_DOC, 0)
    noise = chunk("noise", OTHER_DOC)
    result = ev.evaluate_case(
        case("c", documents=[EXPECTED_DOC], chunks=[target]),
        ranked((noise, 0.9), (target, 0.8)),
    )

    assert (result.any_of_rank, result.chunk_rank, result.document_rank) == (2, 2, 2)


def test_a_different_chunk_of_an_expected_document_is_a_document_hit() -> None:
    target = chunk("target", EXPECTED_DOC, 0)
    sibling = chunk("sibling", EXPECTED_DOC, 1)
    result = ev.evaluate_case(
        case("c", documents=[EXPECTED_DOC], chunks=[target]),
        ranked((sibling, 0.9), (target, 0.5)),
    )

    assert result.any_of_rank == 1  # the sibling counts
    assert result.document_rank == 1
    assert result.chunk_rank == 2  # but the exact chunk is second
    assert result.top[0].document_hit and not result.top[0].chunk_hit


def test_an_expected_chunk_in_an_unexpected_document_is_still_a_hit() -> None:
    elsewhere = chunk("copy", OTHER_DOC)
    result = ev.evaluate_case(
        case("c", documents=[EXPECTED_DOC], chunks=[elsewhere]),
        ranked(
            (elsewhere, 0.9),
        ),
    )

    assert result.any_of_rank == 1
    assert result.chunk_rank == 1
    assert result.document_rank is None


def test_no_evidence_anywhere_in_the_ranking_is_a_miss() -> None:
    noise = [chunk(f"noise {n}", OTHER_DOC, n) for n in range(15)]
    result = ev.evaluate_case(
        case("c", documents=[EXPECTED_DOC], chunks=[chunk("absent", EXPECTED_DOC)]),
        [(c, 1 - n / 100) for n, c in enumerate(noise)],
    )

    assert (result.any_of_rank, result.chunk_rank, result.document_rank) == (
        None,
        None,
        None,
    )
    assert len(result.top) == ev.TOP_RANKS_REPORTED


def test_any_of_takes_the_earliest_of_several_expected_documents() -> None:
    first, second = "https://example.test/a", "https://example.test/b"
    hit_second = chunk("b", second)
    hit_first = chunk("a", first)
    result = ev.evaluate_case(
        case("c", documents=[first, second], chunks=[hit_first, hit_second]),
        ranked((chunk("noise", OTHER_DOC), 0.9), (hit_second, 0.8), (hit_first, 0.7)),
    )

    assert result.any_of_rank == 2


def test_the_top_list_carries_hashes_and_scores_but_no_text() -> None:
    target = chunk("secret chunk body", EXPECTED_DOC)
    result = ev.evaluate_case(
        case("c", documents=[EXPECTED_DOC]),
        ranked(
            (target, 0.7),
        ),
    )

    candidate = result.top[0]
    assert candidate.content_hash == target.content_hash
    assert candidate.similarity == 0.7
    assert "secret chunk body" not in repr(result)
    assert "secret chunk body" not in repr(target)


# ---------------------------------------------------------------------------
# Recall@K and MRR
# ---------------------------------------------------------------------------


def test_recall_at_k_counts_cases_found_within_k() -> None:
    result = ev.metrics([1, 2, 4, 11, None])

    assert result.cases == 5
    assert result.recall_at == {1: 1 / 5, 3: 2 / 5, 5: 3 / 5, 10: 3 / 5}


def test_mrr_averages_reciprocal_first_hit_ranks() -> None:
    result = ev.metrics([1, 2, 4, 11, None])

    assert result.mrr == pytest.approx((1 + 1 / 2 + 1 / 4 + 1 / 11 + 0) / 5)
    assert result.mrr_at_10 == pytest.approx((1 + 1 / 2 + 1 / 4 + 0 + 0) / 5)


def test_a_perfect_ranking_scores_one_everywhere() -> None:
    result = ev.metrics([1, 1, 1])

    assert all(value == 1.0 for value in result.recall_at.values())
    assert result.mrr == 1.0


def test_no_hits_scores_zero_everywhere() -> None:
    result = ev.metrics([None, None])

    assert all(value == 0.0 for value in result.recall_at.values())
    assert result.mrr == 0.0


def test_metrics_of_no_cases_do_not_divide_by_zero() -> None:
    assert ev.metrics([]).mrr == 0.0


def results_for_mixed_cases() -> list[ev.CaseResult]:
    target = chunk("target", EXPECTED_DOC)
    sibling = chunk("sibling", EXPECTED_DOC, 1)
    noise = chunk("noise", OTHER_DOC)
    return [
        ev.evaluate_case(
            case("a1", documents=[EXPECTED_DOC], chunks=[target], category="faq"),
            ranked((target, 0.9), (noise, 0.1)),
        ),
        ev.evaluate_case(
            case("a2", documents=[EXPECTED_DOC], chunks=[target], category="image"),
            ranked((noise, 0.9), (sibling, 0.8), (target, 0.7)),
        ),
        ev.evaluate_case(
            case("u1", answerable=False, category="unanswerable"),
            ranked((noise, 0.55), (target, 0.2)),
        ),
    ]


def test_unanswerable_cases_never_enter_the_metrics() -> None:
    variants = ev.variant_metrics(results_for_mixed_cases())

    assert variants["any_of"].cases == 2
    assert variants["any_of"].recall_at[1] == 0.5  # a1 only
    assert variants["any_of"].recall_at[3] == 1.0
    assert variants["chunk_exact"].recall_at[3] == 1.0
    assert variants["chunk_exact"].recall_at[1] == 0.5


def test_metrics_can_be_split_by_category() -> None:
    by_category = ev.category_metrics(results_for_mixed_cases())

    assert set(by_category) == {"faq", "image"}
    assert by_category["faq"].recall_at[1] == 1.0
    assert by_category["image"].recall_at[1] == 0.0


# ---------------------------------------------------------------------------
# Unanswerable reporting
# ---------------------------------------------------------------------------


def test_the_strong_reference_is_the_weakest_correct_top_result() -> None:
    assert ev.weakest_correct_top1(results_for_mixed_cases()) == 0.9


def test_the_reference_is_absent_when_nothing_was_ranked_first() -> None:
    noise = chunk("noise", OTHER_DOC)
    miss = ev.evaluate_case(
        case("a", documents=[EXPECTED_DOC]),
        ranked(
            (noise, 0.5),
        ),
    )

    assert ev.weakest_correct_top1([miss]) is None


def build_report(results: Sequence[ev.CaseResult]) -> dict[str, object]:
    return ev.build_report(
        results,
        model=ev.MODEL,
        dimensions=ev.DIMENSIONS,
        chunks_embedded=3,
        queries=3,
        api_requests=2,
        prompt_tokens=1234,
        run_requests=2,
        run_prompt_tokens=1234,
        dataset_sha256="d" * 64,
        corpus_sha256="c" * 64,
        integrity={"production_embeddings_after": 0},
        errors=[],
    )


def test_an_unanswerable_case_is_reported_not_scored() -> None:
    report = build_report(results_for_mixed_cases())

    unanswerable = cast(list[dict[str, object]], report["unanswerable_cases"])
    assert [entry["case_id"] for entry in unanswerable] == ["u1"]
    assert unanswerable[0]["top1_similarity"] == 0.55
    # 0.55 is below the weakest correct top-1 (0.9): not flagged as strong.
    assert unanswerable[0]["strong_candidate"] is False
    assert report["misses"] == []  # u1 is not a "miss"
    assert "u1" not in {
        c["case_id"] for c in cast(list[dict[str, object]], report["answerable_cases"])
    }


def test_an_unanswerable_case_at_or_above_the_reference_is_flagged() -> None:
    noise = chunk("noise", OTHER_DOC)
    target = chunk("target", EXPECTED_DOC)
    results = [
        ev.evaluate_case(
            case("a", documents=[EXPECTED_DOC]),
            ranked(
                (target, 0.6),
            ),
        ),
        ev.evaluate_case(
            case("u", answerable=False),
            ranked(
                (noise, 0.7),
            ),
        ),
    ]

    unanswerable = cast(
        list[dict[str, object]], build_report(results)["unanswerable_cases"]
    )

    assert unanswerable[0]["strong_candidate"] is True


def test_the_report_lists_misses_and_late_hits() -> None:
    report = build_report(results_for_mixed_cases())

    late = cast(list[dict[str, object]], report["found_only_below_rank_1"])
    assert [entry["case_id"] for entry in late] == ["a2"]
    assert late[0]["any_of_rank"] == 2


def test_the_markdown_report_has_the_required_figures() -> None:
    text = ev.render_markdown(build_report(results_for_mixed_cases()))

    for expected in (
        "text-embedding-3-small",
        "1536",
        "Recall@1",
        "Recall@10",
        "MRR",
        "1234",
        "u1",
        "a2",
    ):
        assert expected in text


# ---------------------------------------------------------------------------
# Validation of vectors and setup
# ---------------------------------------------------------------------------


def test_complete_well_formed_vectors_validate() -> None:
    vectors = {"a": [1.0, 0.0], "b": [0.0, 1.0]}

    assert ev.validate_vectors(vectors, ["a", "b"], 2) == []


def test_a_missing_vector_is_reported() -> None:
    assert "1 text(s) have no vector" in ev.validate_vectors(
        {"a": [1.0]}, ["a", "b"], 1
    )


def test_a_vector_of_the_wrong_dimension_is_reported() -> None:
    problems = ev.validate_vectors({"a": [1.0, 0.0, 0.0]}, ["a"], 2)

    assert "1 vector(s) do not have 2 dimensions" in problems


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_a_non_finite_vector_is_reported(bad: float) -> None:
    assert any(
        "NaN or infinity" in p for p in ev.validate_vectors({"a": [1.0, bad]}, ["a"], 2)
    )


def test_an_all_zero_vector_is_reported() -> None:
    assert any(
        "all zeros" in p for p in ev.validate_vectors({"a": [0.0, 0.0]}, ["a"], 2)
    )


def test_a_vector_for_no_expected_text_is_reported() -> None:
    assert any(
        "belong to no expected text" in p
        for p in ev.validate_vectors({"a": [1.0], "x": [1.0]}, ["a"], 1)
    )


def golden_for(*cases: ev.GoldenCase, expected_chunks: int) -> ev.Golden:
    return ev.Golden(
        cases=tuple(cases), expected_chunks=expected_chunks, sha256="0" * 64
    )


def setup_problems(
    chunks: Sequence[ev.Chunk],
    golden: ev.Golden,
    *,
    model: str = ev.MODEL,
    dimensions: int = ev.DIMENSIONS,
    production_embeddings: int = 0,
) -> list[str]:
    return ev.validate_setup(
        chunks,
        golden,
        model=model,
        dimensions=dimensions,
        production_embeddings=production_embeddings,
    )


def test_a_consistent_setup_has_no_problems() -> None:
    target = chunk("target", EXPECTED_DOC)

    golden = golden_for(
        case("c", documents=[EXPECTED_DOC], chunks=[target]), expected_chunks=1
    )

    assert setup_problems([target], golden) == []


def test_setup_refuses_an_unsupported_model_or_a_mismatched_dimension() -> None:
    target = chunk("target", EXPECTED_DOC)
    golden = golden_for(
        case("c", documents=[EXPECTED_DOC], chunks=[target]), expected_chunks=1
    )

    unsupported = setup_problems(
        [target], golden, model="text-embedding-ada-002", dimensions=1536
    )
    large_at_small_size = setup_problems(
        [target], golden, model="text-embedding-3-large", dimensions=1536
    )
    small_at_large_size = setup_problems(
        [target], golden, model="text-embedding-3-small", dimensions=3072
    )

    assert any("not a supported evaluation model" in p for p in unsupported)
    assert any("expected 3072" in p for p in large_at_small_size)
    assert any("expected 1536" in p for p in small_at_large_size)


def test_setup_refuses_a_chunk_count_that_differs_from_the_golden_set() -> None:
    target = chunk("target", EXPECTED_DOC)
    golden = golden_for(
        case("c", documents=[EXPECTED_DOC], chunks=[target]), expected_chunks=2
    )

    assert any(
        "1 active chunks selected" in p for p in setup_problems([target], golden)
    )


def test_setup_refuses_empty_chunks_and_bad_hashes() -> None:
    empty = ev.Chunk(hash_content("  "), EXPECTED_DOC, "grc", 0, "  ")
    forged = ev.Chunk("f" * 64, EXPECTED_DOC, "grc", 1, "text")
    golden = golden_for(case("c", documents=[EXPECTED_DOC]), expected_chunks=2)

    problems = setup_problems([empty, forged], golden)

    assert any("empty" in p for p in problems)
    assert any("content_hash" in p for p in problems)


def test_setup_refuses_expected_evidence_that_is_not_in_the_corpus() -> None:
    target = chunk("target", EXPECTED_DOC)
    ghost = chunk("ghost", "https://example.test/ghost")
    golden = golden_for(
        case("c", documents=["https://example.test/ghost"], chunks=[ghost]),
        expected_chunks=1,
    )

    problems = setup_problems([target], golden)

    assert any("not an active document" in p for p in problems)
    assert any("not in the corpus" in p for p in problems)


def test_setup_refuses_when_the_production_embedding_column_is_in_use() -> None:
    target = chunk("target", EXPECTED_DOC)
    golden = golden_for(
        case("c", documents=[EXPECTED_DOC], chunks=[target]), expected_chunks=1
    )

    assert any(
        "production embedding" in p
        for p in setup_problems([target], golden, production_embeddings=3)
    )


def test_setup_refuses_malformed_cases() -> None:
    target = chunk("target", EXPECTED_DOC)
    golden = golden_for(
        case("answerable-without-evidence"),
        case("unanswerable-with-evidence", documents=[EXPECTED_DOC], answerable=False),
        expected_chunks=1,
    )

    problems = setup_problems([target], golden)

    assert any("answerable case has no expected document" in p for p in problems)
    assert any("unanswerable case lists expected evidence" in p for p in problems)


# ---------------------------------------------------------------------------
# The artifact
# ---------------------------------------------------------------------------


def store_in(directory: Path, dimensions: int = 4) -> ev.VectorStore:
    return ev.VectorStore(directory, model="m", dimensions=dimensions)


def test_a_new_artifact_is_marked_evaluation_only(tmp_path: Path) -> None:
    _ = store_in(tmp_path)

    manifest = json.loads((tmp_path / "manifest.json").read_text("utf-8"))
    assert manifest["evaluation_only"] is True
    assert "document_chunks.embedding" in manifest["note"]
    assert manifest["model"] == "m" and manifest["dimensions"] == 4


def test_vectors_round_trip_through_the_artifact(tmp_path: Path) -> None:
    store = store_in(tmp_path)
    store.append(["a", "b"], [[1.0, 0.5, 0.25, 0.125], [0.0, 1.0, 0.0, 2.0]])

    loaded = store_in(tmp_path).load()

    assert list(loaded["a"]) == [1.0, 0.5, 0.25, 0.125]
    assert list(loaded["b"]) == [0.0, 1.0, 0.0, 2.0]


def test_a_reopened_artifact_remembers_what_it_holds(tmp_path: Path) -> None:
    store_in(tmp_path).append(["a"], [[1.0, 0.0, 0.0, 0.0]])

    assert store_in(tmp_path).keys == frozenset({"a"})


def test_usage_accumulates_across_reopened_runs(tmp_path: Path) -> None:
    store_in(tmp_path).add_usage(requests=3, prompt_tokens=300)
    reopened = store_in(tmp_path)
    reopened.add_usage(requests=2, prompt_tokens=50)

    final = store_in(tmp_path)
    assert (final.api_requests, final.prompt_tokens) == (5, 350)


def test_a_vector_without_an_index_line_is_discarded_on_reopen(tmp_path: Path) -> None:
    """A crash between the two writes must not leave a mislabelled vector."""
    store = store_in(tmp_path)
    store.append(["a"], [[1.0, 0.0, 0.0, 0.0]])
    with (tmp_path / "vectors.f32").open("ab") as handle:
        array("f", [9.0, 9.0, 9.0, 9.0]).tofile(handle)

    reopened = store_in(tmp_path)

    assert reopened.keys == frozenset({"a"})
    assert (tmp_path / "vectors.f32").stat().st_size == 4 * 4
    reopened.append(["b"], [[2.0, 0.0, 0.0, 0.0]])
    assert list(reopened.load()["b"]) == [2.0, 0.0, 0.0, 0.0]


def test_a_partial_vector_row_is_discarded_on_reopen(tmp_path: Path) -> None:
    store = store_in(tmp_path)
    store.append(["a"], [[1.0, 0.0, 0.0, 0.0]])
    with (tmp_path / "vectors.f32").open("ab") as handle:
        handle.write(b"\x00\x00\x80")  # a torn write

    assert store_in(tmp_path).keys == frozenset({"a"})
    assert (tmp_path / "vectors.f32").stat().st_size == 4 * 4


def test_an_index_line_without_a_vector_is_discarded_on_reopen(tmp_path: Path) -> None:
    store = store_in(tmp_path)
    store.append(["a"], [[1.0, 0.0, 0.0, 0.0]])
    with (tmp_path / "index.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"key": "ghost"}) + "\n" + '{"key": "tor')

    assert store_in(tmp_path).keys == frozenset({"a"})


def test_an_artifact_for_another_model_or_dimension_is_refused(tmp_path: Path) -> None:
    store_in(tmp_path)

    with pytest.raises(ValueError, match="different model or dimension"):
        _ = ev.VectorStore(tmp_path, model="m", dimensions=8)
    with pytest.raises(ValueError, match="different model or dimension"):
        _ = ev.VectorStore(tmp_path, model="other", dimensions=4)


def test_the_artifact_refuses_malformed_or_repeated_vectors(tmp_path: Path) -> None:
    store = store_in(tmp_path)
    store.append(["a"], [[1.0, 0.0, 0.0, 0.0]])

    with pytest.raises(ValueError, match="malformed"):
        store.append(["b"], [[1.0, 0.0, 0.0]])
    with pytest.raises(ValueError, match="malformed"):
        store.append(["c"], [[1.0, math.nan, 0.0, 0.0]])
    with pytest.raises(ValueError, match="already stored"):
        store.append(["a"], [[1.0, 0.0, 0.0, 0.0]])
    with pytest.raises(ValueError, match="already stored"):
        store.append(["d", "d"], [[1.0, 0.0, 0.0, 0.0]] * 2)
    assert store.keys == frozenset({"a"})  # nothing partial was written


# ---------------------------------------------------------------------------
# Embedding with an Embedder
# ---------------------------------------------------------------------------


def golden_two_cases() -> ev.Golden:
    return golden_for(
        case("c1", documents=[EXPECTED_DOC]),
        case("c2", documents=[OTHER_DOC]),
        expected_chunks=3,
    )


def test_texts_are_unique_and_queries_come_first() -> None:
    chunks = [chunk("one", "u"), chunk("one", "u2"), chunk("two", "u")]

    texts = ev.texts_to_embed(chunks, golden_two_cases())

    assert list(texts.values()) == ["query for c1", "query for c2", "one", "two"]
    assert list(texts) == [hash_content(t) for t in texts.values()]


@pytest.mark.anyio
async def test_only_missing_texts_are_embedded_in_batches(tmp_path: Path) -> None:
    store = store_in(tmp_path, dimensions=8)
    fake = FakeEmbedder(dimensions=8)
    texts = {hash_content(t): t for t in ("a", "b", "c", "d", "e")}

    first = await ev.embed_missing(store, fake, texts, batch_size=2)

    assert [len(batch) for batch in fake.calls] == [2, 2, 1]
    assert (first.embedded, first.requests) == (5, 3)
    assert first.prompt_tokens is None  # a fake reports no usage

    again = await ev.embed_missing(store, fake, texts, batch_size=2)

    assert (again.embedded, again.requests) == (0, 0)
    assert len(fake.calls) == 3  # no further calls at all


@pytest.mark.anyio
async def test_an_interrupted_run_resumes_without_repeating_work(
    tmp_path: Path,
) -> None:
    texts = {hash_content(t): t for t in ("a", "b", "c", "d")}

    class FailsOnSecondBatch(FakeEmbedder):
        async def embed(self, texts: Sequence[str]) -> list[list[float]]:
            if len(self.calls) == 1:
                self.calls.append(list(texts))
                raise TimeoutError("simulated outage")
            return await super().embed(texts)

    store = store_in(tmp_path, dimensions=8)
    with pytest.raises(TimeoutError):
        _ = await ev.embed_missing(
            store, FailsOnSecondBatch(dimensions=8), texts, batch_size=2
        )
    assert len(store_in(tmp_path, dimensions=8).keys) == 2  # batch one survived

    resumed = FakeEmbedder(dimensions=8)
    result = await ev.embed_missing(
        store_in(tmp_path, dimensions=8), resumed, texts, batch_size=2
    )

    assert result.embedded == 2
    assert [t for batch in resumed.calls for t in batch] == ["c", "d"]


def stub_openai_embedder(
    tokens: int, dimensions: int = 4, reply_dimensions: int | None = None
) -> tuple[OpenAIEmbedder, AsyncMock]:
    """An embedder over a stub client; ``reply_dimensions`` makes it answer wrongly."""
    answer_width = dimensions if reply_dimensions is None else reply_dimensions

    def reply(*, input: list[str], **_: object) -> CreateEmbeddingResponse:
        return CreateEmbeddingResponse(
            data=[
                Embedding(
                    embedding=[float(n + 1)] * answer_width, index=n, object="embedding"
                )
                for n in range(len(input))
            ],
            model="m",
            object="list",
            usage=Usage(prompt_tokens=tokens, total_tokens=tokens),
        )

    create = AsyncMock(side_effect=reply)
    client = cast(
        AsyncOpenAI, SimpleNamespace(embeddings=SimpleNamespace(create=create))
    )
    return OpenAIEmbedder(client, model="m", dimensions=dimensions), create


@pytest.mark.anyio
async def test_api_requests_and_tokens_are_recorded_in_the_artifact(
    tmp_path: Path,
) -> None:
    store = store_in(tmp_path)
    embedder, create = stub_openai_embedder(tokens=100)
    texts = {hash_content(t): t for t in ("a", "b", "c")}

    stats = await ev.embed_missing(store, embedder, texts, batch_size=2)

    assert create.await_count == 2
    assert (stats.requests, stats.prompt_tokens) == (2, 200)
    reopened = store_in(tmp_path)
    assert (reopened.api_requests, reopened.prompt_tokens) == (2, 200)


@pytest.mark.anyio
async def test_spend_is_recorded_even_when_the_answer_is_unusable(
    tmp_path: Path,
) -> None:
    store = store_in(tmp_path)
    embedder, _ = stub_openai_embedder(tokens=70, dimensions=4, reply_dimensions=3)

    with pytest.raises(EmbeddingError, match="3 dimensions; expected 4"):
        _ = await ev.embed_missing(
            store, embedder, {hash_content("a"): "a"}, batch_size=1
        )

    assert (store_in(tmp_path).api_requests, store_in(tmp_path).prompt_tokens) == (
        1,
        70,
    )
    assert store_in(tmp_path).keys == frozenset()


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------

DOC_A, DOC_B, DOC_C = (f"https://example.test/{name}" for name in ("a", "b", "c"))
CORPUS = (
    chunk("alpha body", DOC_A, 0),
    chunk("beta body", DOC_A, 1),
    chunk("gamma body", DOC_B, 0),
    chunk("delta body", DOC_C, 0),
)
SCRIPT: dict[str, dict[int, float]] = {
    "alpha body": {0: 1.0},
    "beta body": {0: 0.5, 1: 0.5},
    "gamma body": {2: 1.0},
    "delta body": {3: 1.0},
    "find alpha": {0: 1.0},
    "find gamma": {3: 1.0},  # deliberately points at the wrong chunk
    "unrelated question": {3: 1.0},
}


def write_golden(path: Path, *, chunks: int = len(CORPUS)) -> Path:
    cases = [
        {
            "id": "q1",
            "category": "faq",
            "query": "find alpha",
            "answerable": True,
            "expected_document_uris": [DOC_A],
            "expected_chunk_hashes": [CORPUS[0].content_hash],
            "expected_source_scopes": ["grc"],
            "notes": "n",
        },
        {
            "id": "q2",
            "category": "paraphrase",
            "query": "find gamma",
            "answerable": True,
            "expected_document_uris": [DOC_B],
            "expected_chunk_hashes": [CORPUS[2].content_hash],
            "expected_source_scopes": ["grc"],
            "notes": "n",
        },
        {
            "id": "q3",
            "category": "unanswerable",
            "query": "unrelated question",
            "answerable": False,
            "expected_document_uris": [],
            "expected_chunk_hashes": [],
            "expected_source_scopes": [],
            "absent_terms": ["x"],
            "notes": "n",
        },
    ]
    path.write_text(
        json.dumps({"version": 1, "built_against": {"chunks": chunks}, "cases": cases}),
        "utf-8",
    )
    return path


def must_not_be_called() -> Embedder:
    pytest.fail("the embedder factory was called")


def run_cli(
    tmp_path: Path,
    *flags: str,
    embedder: Embedder | None = None,
    corpus: ev.CorpusSnapshot | None = None,
    loader_calls: list[int] | None = None,
    golden_chunks: int = len(CORPUS),
) -> int:
    snapshot = corpus or ev.CorpusSnapshot(CORPUS, 0)

    def loader() -> ev.CorpusSnapshot:
        if loader_calls is not None:
            loader_calls.append(1)
        return snapshot

    factory = must_not_be_called if embedder is None else (lambda: embedder)
    return ev.main(
        [
            "--dataset",
            str(write_golden(tmp_path / "golden.json", chunks=golden_chunks)),
            "--artifact-dir",
            str(tmp_path / "artifact"),
            *flags,
        ],
        corpus_loader=loader,
        embedder_factory=factory,
    )


def test_without_the_api_flag_nothing_is_embedded_and_no_embedder_is_built(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status = run_cli(tmp_path)

    output = capsys.readouterr().out
    assert status == 0
    assert "No API call made" in output
    assert "Still to embed: 4 chunks + 3 queries" in output
    assert "Setup validation passed" in output
    assert not (tmp_path / "artifact" / "report.json").exists()


def test_a_failed_setup_validation_stops_before_any_embedder_exists(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status = run_cli(
        tmp_path, "--run-api", golden_chunks=99
    )  # factory would fail the test

    assert status == 1
    assert "SETUP VALIDATION FAILED" in capsys.readouterr().out


def test_a_used_production_embedding_column_stops_the_run(tmp_path: Path) -> None:
    status = run_cli(tmp_path, "--run-api", corpus=ev.CorpusSnapshot(CORPUS, 5))

    assert status == 1


def test_a_full_run_embeds_evaluates_and_writes_the_report(tmp_path: Path) -> None:
    embedder = ScriptedEmbedder(SCRIPT)

    status = run_cli(tmp_path, "--run-api", "--batch-size", "4", embedder=embedder)

    assert status == 0
    artifact = tmp_path / "artifact"
    report = json.loads((artifact / "report.json").read_text("utf-8"))
    assert report["evaluation_only"] is True
    assert (report["chunks_embedded"], report["queries"]) == (4, 3)
    cases = {c["case_id"]: c for c in report["answerable_cases"]}
    assert cases["q1"]["any_of_rank"] == 1
    assert cases["q2"]["any_of_rank"] in (
        2,
        3,
        4,
    )  # tied with alpha/beta at similarity 0
    assert report["metrics"]["any_of"]["recall_at"]["1"] == 0.5
    assert [c["case_id"] for c in report["unanswerable_cases"]] == ["q3"]
    assert report["unanswerable_cases"][0]["top1_similarity"] == pytest.approx(1.0)
    assert report["integrity"]["production_embeddings_after"] == 0
    assert report["integrity"]["corpus_unchanged_during_run"] is True
    assert (artifact / "report.md").exists()
    # Queries lead the first request, ahead of every chunk.
    assert embedder.calls[0] == [
        "find alpha",
        "find gamma",
        "unrelated question",
        "alpha body",
    ]


def test_the_report_and_artifact_never_contain_chunk_text(tmp_path: Path) -> None:
    _ = run_cli(tmp_path, "--run-api", embedder=ScriptedEmbedder(SCRIPT))

    for path in (tmp_path / "artifact").iterdir():
        if path.suffix in {".json", ".md", ".jsonl"}:
            text = path.read_text("utf-8")
            assert not any(c.content in text for c in CORPUS), path.name


def test_a_completed_artifact_is_re_evaluated_offline_with_no_embedder(
    tmp_path: Path,
) -> None:
    _ = run_cli(tmp_path, "--run-api", embedder=ScriptedEmbedder(SCRIPT))
    first = (tmp_path / "artifact" / "report.json").read_text("utf-8")

    status = run_cli(tmp_path)  # no flag, and the factory fails the test if used

    assert status == 0
    second = json.loads((tmp_path / "artifact" / "report.json").read_text("utf-8"))
    assert second["api_requests_this_run"] == 0
    assert second["metrics"] == json.loads(first)["metrics"]


def test_a_second_api_run_has_nothing_left_to_embed(tmp_path: Path) -> None:
    _ = run_cli(tmp_path, "--run-api", embedder=ScriptedEmbedder(SCRIPT))
    second = ScriptedEmbedder(SCRIPT)

    status = run_cli(tmp_path, "--run-api", embedder=second)

    assert status == 0
    assert second.calls == []


def test_a_provider_failure_keeps_progress_and_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class Failing:
        calls = 0

        async def embed(self, texts: Sequence[str]) -> list[list[float]]:
            self.calls += 1
            if self.calls == 2:
                raise TimeoutError("secret-looking provider text")
            return [sparse(SCRIPT[t]) for t in texts]

    status = run_cli(tmp_path, "--run-api", "--batch-size", "3", embedder=Failing())

    out = capsys.readouterr().out
    assert status == 1
    assert "EMBEDDING STOPPED: TimeoutError" in out
    assert "secret-looking provider text" not in out  # only the class is printed
    assert (
        len(
            ev.VectorStore(
                tmp_path / "artifact", model=ev.MODEL, dimensions=ev.DIMENSIONS
            ).keys
        )
        == 3
    )


def test_a_corpus_that_changes_during_the_run_fails_the_run(tmp_path: Path) -> None:
    snapshots = iter([ev.CorpusSnapshot(CORPUS, 0), ev.CorpusSnapshot(CORPUS[:3], 0)])

    status = ev.main(
        [
            "--dataset",
            str(write_golden(tmp_path / "golden.json")),
            "--artifact-dir",
            str(tmp_path / "artifact"),
            "--run-api",
        ],
        corpus_loader=lambda: next(snapshots),
        embedder_factory=lambda: ScriptedEmbedder(SCRIPT),
    )

    assert status == 1


def test_the_evaluation_pins_its_model_and_dimensions() -> None:
    assert ev.MODEL == "text-embedding-3-small"
    assert ev.DIMENSIONS == 1536
    assert ev.PROFILES == {
        "text-embedding-3-small": 1536,
        "text-embedding-3-large": 3072,
    }


# ---------------------------------------------------------------------------
# Model selection: the Large profile
# ---------------------------------------------------------------------------

LARGE = "text-embedding-3-large"
LARGE_DIMENSIONS = 3072


def test_each_model_has_its_own_artifact_directory() -> None:
    small = ev.default_artifact_dir("text-embedding-3-small")
    large = ev.default_artifact_dir(LARGE)

    assert small.name == "embedding-small-1536"
    assert large.name == "embedding-large-3072"
    assert small != large and small.parent == large.parent


def test_the_two_profiles_cannot_share_an_artifact(tmp_path: Path) -> None:
    _ = ev.VectorStore(tmp_path, model="text-embedding-3-small", dimensions=1536)

    with pytest.raises(ValueError, match="different model or dimension"):
        _ = ev.VectorStore(tmp_path, model=LARGE, dimensions=LARGE_DIMENSIONS)


def test_both_profiles_validate_at_their_own_dimension() -> None:
    target = chunk("target", EXPECTED_DOC)
    golden = golden_for(
        case("c", documents=[EXPECTED_DOC], chunks=[target]), expected_chunks=1
    )

    for model, dimensions in ev.PROFILES.items():
        assert (
            setup_problems([target], golden, model=model, dimensions=dimensions) == []
        )


def test_the_embedder_is_pinned_to_the_selected_model_whatever_settings_say() -> None:
    from pydantic import SecretStr

    from preston.core.config import Settings

    # Settings that say Small must not change what a Large evaluation measures.
    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        openai_api_key=SecretStr("sk-test-not-a-real-key-0000"),
        embedding_model="text-embedding-3-small",
        embedding_dimensions=1536,
    )

    embedder = ev.embedder_for(LARGE, settings)

    assert isinstance(embedder, OpenAIEmbedder)
    assert (embedder.model, embedder.dimensions) == (LARGE, LARGE_DIMENSIONS)
    assert settings.embedding_model == "text-embedding-3-small"  # settings untouched


def test_an_unsupported_model_is_refused_by_the_command_line() -> None:
    with pytest.raises(SystemExit):
        _ = ev.main(
            ["--model", "text-embedding-ada-002"],
            corpus_loader=lambda: pytest.fail("loaded"),
        )


def test_the_large_profile_validates_without_an_api_call(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status = run_cli(tmp_path, "--model", LARGE)

    output = capsys.readouterr().out
    assert status == 0
    assert f"Model: {LARGE} (3072 dimensions)" in output
    assert "Expected total vectors: 7" in output  # 4 chunks + 3 queries
    assert "No API call made" in output


def test_a_full_large_run_uses_large_dimensions_throughout(tmp_path: Path) -> None:
    embedder = ScriptedEmbedder(SCRIPT, dimensions=LARGE_DIMENSIONS)

    status = run_cli(tmp_path, "--model", LARGE, "--run-api", embedder=embedder)

    assert status == 0
    artifact = tmp_path / "artifact"
    manifest = json.loads((artifact / "manifest.json").read_text("utf-8"))
    assert (manifest["model"], manifest["dimensions"]) == (LARGE, LARGE_DIMENSIONS)
    assert manifest["evaluation_only"] is True
    assert (artifact / "vectors.f32").stat().st_size == 7 * LARGE_DIMENSIONS * 4
    report = json.loads((artifact / "report.json").read_text("utf-8"))
    assert (report["model"], report["dimensions"]) == (LARGE, LARGE_DIMENSIONS)
    assert report["metrics"]["any_of"]["recall_at"]["1"] == 0.5


def test_large_refuses_vectors_of_the_small_dimension(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    wrong = ScriptedEmbedder(SCRIPT, dimensions=ev.DIMENSIONS)  # 1536, not 3072

    status = run_cli(tmp_path, "--model", LARGE, "--run-api", embedder=wrong)

    assert status == 1
    assert "EMBEDDING STOPPED" in capsys.readouterr().out
    store = ev.VectorStore(
        tmp_path / "artifact", model=LARGE, dimensions=LARGE_DIMENSIONS
    )
    assert store.keys == frozenset()  # nothing malformed was kept


def test_the_report_estimates_cost_from_the_list_price() -> None:
    def cost(model: str) -> object:
        report = ev.build_report(
            [],
            model=model,
            dimensions=ev.PROFILES.get(model, 0),
            chunks_embedded=0,
            queries=0,
            api_requests=1,
            prompt_tokens=2_000_000,
            run_requests=1,
            run_prompt_tokens=2_000_000,
            dataset_sha256="d" * 64,
            corpus_sha256="c" * 64,
            integrity={},
            errors=[],
        )
        return report["estimated_cost_usd"]

    assert cost("text-embedding-3-small") == pytest.approx(0.04)
    assert cost(LARGE) == pytest.approx(0.26)
    assert cost("some-other-model") is None


def test_the_openai_embedder_exposes_usage_counters() -> None:
    embedder, _ = stub_openai_embedder(tokens=5)

    assert (embedder.requests, embedder.prompt_tokens) == (0, 0)
