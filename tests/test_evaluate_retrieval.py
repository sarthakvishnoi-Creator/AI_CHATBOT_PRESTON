"""Tests for ``scripts/evaluate_retrieval.py``: where evidence sits in a returned list.

Pure logic on invented candidates; no database, no artifacts, no network.
"""

import sys
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPOSITORY_ROOT / "scripts"))

import evaluate_embeddings as ev
import evaluate_retrieval as er

from preston.canonical import hash_content
from preston.retrieval import Candidate, RetrievalLimits

A, B = "https://www.intercert.com/a", "https://www.intercert.com/b"


def item(uri: str, index: int, score: float, scope: str = "grc") -> Candidate:
    return Candidate(hash_content(f"{uri}#{index}"), uri, scope, index, score)


def case(answerable: bool = True) -> ev.GoldenCase:
    return ev.GoldenCase(
        id="c",
        category="faq",
        query="q",
        answerable=answerable,
        expected_document_uris=frozenset({A}) if answerable else frozenset(),
        expected_chunk_hashes=frozenset({hash_content(f"{A}#1")})
        if answerable
        else frozenset(),
    )


def test_positions_of_chunk_document_and_document_order() -> None:
    returned = [item(B, 0, 0.9, "blog"), item(A, 0, 0.8), item(A, 1, 0.7)]

    result = er.outcome(case(), returned, frozenset({"grc"}))

    assert result.any_of_rank == 2  # first chunk of the expected document
    assert result.chunk_rank == 3  # the exact supporting chunk
    assert result.document_position == 2  # second distinct document
    assert result.top_scope_expected is False  # a blog came first
    assert (result.top_lane, result.returned) == ("blog", 3)


def test_evidence_that_is_not_returned_is_a_miss() -> None:
    result = er.outcome(case(), [item(B, 0, 0.9)], frozenset({"grc"}))

    assert (result.chunk_rank, result.any_of_rank, result.document_position) == (
        None,
        None,
        None,
    )


def test_an_unanswerable_case_records_only_what_came_back() -> None:
    result = er.outcome(case(answerable=False), [item(B, 0, 0.42, "blog")], frozenset())

    assert result.top_scope_expected is None
    assert (result.top_score, result.top_lane) == (0.42, "blog")


def test_the_strategy_runs_the_shipped_selection() -> None:
    """The evaluator measures preston.retrieval.select, not a copy of it."""
    golden = ev.Golden(cases=(case(),), expected_chunks=3, sha256="0" * 64)
    candidates = {
        "c": {
            "intercert": [item(A, 0, 0.8), item(A, 1, 0.7), item(A, 2, 0.6)],
            "blog": [item(B, 0, 0.9, "blog")],
        }
    }
    limits = RetrievalLimits(
        documents_per_lane={"intercert": 1, "blog": 1}, chunks_per_document=2
    )

    (result,) = er.run_strategy(golden, candidates, {"c": frozenset({"grc"})}, limits)

    assert result.returned == 3  # one blog chunk + A's best two chunks
    assert result.chunk_rank == 3
