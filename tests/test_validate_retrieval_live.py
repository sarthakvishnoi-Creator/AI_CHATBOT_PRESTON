"""Tests for ``scripts/validate_retrieval_live.py``.

Pure logic on invented candidates, evidence and timings: no database, no
network, no saved artifacts. The report-level tests prove the report holds
no chunk text.
"""

import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPOSITORY_ROOT / "scripts"))

import evaluate_embeddings as ev
import evaluate_retrieval as er
import validate_retrieval_live as vl

from preston.canonical import hash_content
from preston.embed_chunks import IDENTITY
from preston.retrieval import Candidate, Evidence

A, B, C = (f"https://www.intercert.com/{name}" for name in "abc")
BODY = "SECRET CHUNK BODY that must never reach a report"


def cand(uri: str, index: int, score: float, scope: str = "grc") -> Candidate:
    return Candidate(hash_content(f"{uri}#{index}"), uri, scope, index, score)


def evidence(
    uri: str, index: int, score: float, *, title: str = "Title", rank: int = 1
) -> Evidence:
    return Evidence(
        text=BODY,
        chunk_content_hash=hash_content(f"{uri}#{index}"),
        canonical_uri=uri,
        title=title,
        source_scope="grc",
        content_type="service",
        chunk_index=index,
        retrieval_method="vector",
        rank=rank,
        citation_uri=uri,
        score=score,
        embedding_model=IDENTITY,
    )


def office(index: int) -> Evidence:
    return Evidence(
        text=BODY,
        chunk_content_hash=hash_content(f"office#{index}"),
        canonical_uri="https://www.intercert.com/contact",
        title="Offices",
        source_scope="office_locations",
        content_type="page",
        chunk_index=index,
        retrieval_method="structured",
        rank=index + 1,
        citation_uri=None,
    )


# ---------------------------------------------------------------------------
# Measurement helpers
# ---------------------------------------------------------------------------


def test_percentile_is_nearest_rank() -> None:
    values = [float(n) for n in range(1, 101)]

    assert vl.percentile(values, 50) == 50
    assert vl.percentile(values, 95) == 95
    assert vl.percentile([3.0], 95) == 3.0
    with pytest.raises(ValueError):
        vl.percentile([], 50)


def test_distribution_reports_p50_p95_max_and_mean() -> None:
    result = vl.distribution([1.0, 2.0, 3.0, 4.0])

    assert result == {"p50": 2.0, "p95": 4.0, "max": 4.0, "mean": 2.5}


def test_replica_applies_the_production_grouping() -> None:
    chunks = [
        ev.Chunk(hash_content(f"{n}#{i}"), f"https://x/{n}", "grc", i, "t")
        for n in range(12)
        for i in range(3)
    ]
    vectors = {
        c.content_hash: [1.0, 0.1 * int(c.document_uri[-2:].strip("/"))]
        for c in chunks[:0]
    }
    # Documents closer to the query vector (1, 0) rank first.
    vectors = {
        c.content_hash: [
            1.0,
            0.05 * int(c.document_uri.rsplit("/", 1)[1]) + 0.01 * c.chunk_index,
        ]
        for c in chunks
    }

    returned = vl.float32_replica([1.0, 0.0], chunks, vectors)

    documents = list(dict.fromkeys(c.canonical_uri for c in returned))
    assert len(documents) == vl.DEFAULT_DOCUMENTS
    assert all(
        sum(c.canonical_uri == d for c in returned) <= vl.CHUNKS_PER_DOCUMENT
        for d in documents
    )
    assert documents[0] == "https://x/0"
    scores = [c.score for c in returned if c.canonical_uri == documents[0]]
    assert scores == sorted(scores, reverse=True)


def test_identical_rankings_do_not_disagree() -> None:
    live = [cand(A, 0, 0.9), cand(B, 0, 0.8)]
    offline = [cand(A, 0, 0.9001), cand(B, 0, 0.8001)]

    result = vl.compare("c", live, offline)

    assert not result.disagrees
    assert result.same_order and result.top1_same_chunk
    assert result.top1_score_delta == pytest.approx(0.0001)


def test_a_different_top_chunk_is_a_disagreement() -> None:
    result = vl.compare("c", [cand(A, 1, 0.9)], [cand(A, 0, 0.9)])

    assert result.disagrees
    assert result.top1_same_document and not result.top1_same_chunk
    assert result.top1_score_delta is None


def test_a_different_document_set_is_a_disagreement() -> None:
    result = vl.compare(
        "c", [cand(A, 0, 0.9), cand(B, 0, 0.8)], [cand(A, 0, 0.9), cand(C, 0, 0.8)]
    )

    assert result.disagrees
    assert result.document_overlap == pytest.approx(1 / 3)


def test_a_reordering_alone_is_not_a_disagreement() -> None:
    live = [cand(A, 0, 0.9), cand(B, 0, 0.8), cand(C, 0, 0.7)]
    offline = [cand(A, 0, 0.9), cand(C, 0, 0.8), cand(B, 0, 0.7)]

    result = vl.compare("c", live, offline)

    assert not result.disagrees
    assert not result.same_order


def test_clean_evidence_has_no_safety_problems() -> None:
    assert vl.safety_problems([evidence(A, 0, 0.9)]) == []


def test_an_internal_id_in_a_field_is_flagged() -> None:
    leaked = evidence(A, 0, 0.9, title="Doc 123e4567-e89b-12d3-a456-426614174000")

    assert vl.safety_problems([leaked]) == ["title contains an internal id"]


def test_evidence_exposes_no_id_field() -> None:
    names = vl.evidence_field_names()

    assert not [n for n in names if n == "id" or n.endswith("_id")]


# ---------------------------------------------------------------------------
# Quality summary
# ---------------------------------------------------------------------------


def golden_case(
    case_id: str,
    category: str,
    *,
    answerable: bool = True,
    uri: str = A,
    index: int = 0,
) -> ev.GoldenCase:
    return ev.GoldenCase(
        id=case_id,
        category=category,
        query=f"query {case_id}",
        answerable=answerable,
        expected_document_uris=frozenset({uri}) if answerable else frozenset(),
        expected_chunk_hashes=frozenset({hash_content(f"{uri}#{index}")})
        if answerable
        else frozenset(),
    )


def test_summary_counts_document_and_chunk_hits_by_category() -> None:
    cases = [
        golden_case("f1", "faq"),
        golden_case("f2", "faq"),
        golden_case("u", "unanswerable", answerable=False),
    ]
    outcomes = [
        er.outcome(cases[0], [cand(A, 0, 0.9)], frozenset({"grc"})),
        er.outcome(cases[1], [cand(B, 0, 0.9), cand(A, 1, 0.8)], frozenset({"grc"})),
        er.outcome(cases[2], [cand(B, 0, 0.5)], frozenset()),
    ]

    result = vl.summarize(outcomes)

    assert result["overall"]["cases"] == 2
    assert result["overall"]["document_hits"] == 2
    assert result["overall"]["chunk_hits"] == 1
    assert result["by_category"]["faq"]["document_recall_at"]["1"] == 0.5
    assert "unanswerable" not in result["by_category"]


def test_regressions_list_only_evidence_the_baseline_found() -> None:
    case = golden_case("f1", "faq")
    found = er.outcome(case, [cand(A, 0, 0.9)], frozenset({"grc"}))
    missed = er.outcome(case, [cand(B, 0, 0.9)], frozenset({"grc"}))

    assert vl.regressions([missed], [found])[0]["lost_document"] is True
    assert vl.regressions([found], [missed]) == []
    assert vl.regressions([found], [found]) == []


def test_plan_summary_keeps_timings_nodes_and_buffers() -> None:
    root = {
        "Planning Time": 0.5,
        "Execution Time": 12.5,
        "Plan": {
            "Node Type": "Limit",
            "Shared Hit Blocks": 10,
            "Shared Read Blocks": 0,
            "Plans": [{"Node Type": "Sort", "Plans": [{"Node Type": "Seq Scan"}]}],
        },
    }

    assert vl.summarize_plan(root) == {
        "planning_ms": 0.5,
        "execution_ms": 12.5,
        "nodes": ["Limit", "Sort", "Seq Scan"],
        "shared_hit_blocks": 10,
        "shared_read_blocks": 0,
    }


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def live_case(case_id: str, items: list[Evidence], total: float = 0.4) -> vl.LiveCase:
    return vl.LiveCase(
        case_id=case_id,
        evidence=items,
        embed_seconds=total / 2,
        total_seconds=total,
        query_cosine=0.9999,
        explain={
            "warmup": {"execution_ms": 20.0, "nodes": ["Sort"]},
            "measured": {"execution_ms": 10.0, "nodes": ["Sort"]},
        },
    )


def build(total: float = 0.4) -> dict[str, Any]:
    cases = (
        golden_case("f1", "faq"),
        golden_case("u1", "unanswerable", answerable=False),
        golden_case(
            "s1", "structured", uri="https://www.intercert.com/contact", index=0
        ),
    )
    golden = ev.Golden(cases=cases, expected_chunks=3, sha256="a" * 64)
    offices = [office(0), office(1), office(2)]
    office_case = ev.GoldenCase(
        id="s1",
        category="structured",
        query="query s1",
        answerable=True,
        expected_document_uris=frozenset({"https://www.intercert.com/contact"}),
        expected_chunk_hashes=frozenset({offices[0].chunk_content_hash}),
    )
    golden = ev.Golden(
        cases=(cases[0], cases[1], office_case), expected_chunks=3, sha256="a" * 64
    )
    run = vl.LiveRun(
        cases=[
            live_case("f1", [evidence(A, 0, 0.9)], total),
            live_case("u1", [evidence(B, 0, 0.4)], total),
            live_case("s1", [evidence(C, 0, 0.3)], total),
        ],
        structured=offices,
        structured_seconds=0.01,
        structured_embed_requests=0,
        requests=3,
        prompt_tokens=30,
    )
    offline = {
        "f1": [cand(A, 0, 0.9)],
        "u1": [cand(B, 0, 0.4)],
        "s1": [cand(C, 0, 0.3)],
    }
    scopes = {c.id: frozenset({"grc"}) for c in golden.cases}
    return vl.build_report(
        golden, run, offline, scopes, corpus={"chunks": 3, "embedded": 3}
    )


def test_report_gates_pass_for_a_matching_run() -> None:
    gates = build()["gates"]

    statuses = {name: gate["status"] for name, gate in gates.items()}
    assert statuses == {
        "quality_document_hit_not_worse": "PASS",
        "image_no_regression": "PASS",
        "fidelity_top1": "PASS",
        "latency_p95_end_to_end": "PASS",
        "safety": "PASS",
        "structured_office_locations": "PASS",
    }


def test_structured_cases_stay_out_of_the_vector_metrics() -> None:
    report = build()

    quality = report["quality"]
    assert quality["live"]["overall"]["cases"] == 1
    structured = next(c for c in report["cases"] if c["id"] == "s1")
    assert structured["in_vector_metrics"] is False


def test_a_slow_p95_fails_the_latency_gate() -> None:
    gate = build(total=1.5)["gates"]["latency_p95_end_to_end"]

    assert gate["status"] == "FAIL"


def test_cost_is_computed_from_reported_prompt_tokens() -> None:
    usage = build()["embedding_usage"]

    assert usage["prompt_tokens"] == 30
    assert usage["cost_usd"] == pytest.approx(30 * 0.13 / 1_000_000, abs=1e-9)


def test_reports_never_contain_chunk_text() -> None:
    report = build()

    assert BODY not in json.dumps(report)
    assert BODY not in vl.render_markdown(report)


def test_markdown_lists_every_gate_and_case() -> None:
    markdown = vl.render_markdown(build())

    for name in ("safety", "latency_p95_end_to_end", "fidelity_top1"):
        assert name in markdown
    assert (
        "| f1 |" in markdown
        and "| u1 |" in markdown
        and "(office_locations)" in markdown
    )


# ---------------------------------------------------------------------------
# Wrappers and entry point
# ---------------------------------------------------------------------------


class Inner:
    @property
    def requests(self) -> int:
        return 1

    @property
    def prompt_tokens(self) -> int:
        return 7

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[0.5, 0.5] for _ in texts]


@pytest.mark.anyio
async def test_timed_embedder_records_duration_and_vector() -> None:
    timed = vl.TimedEmbedder(Inner())

    vectors = await timed.embed(["q"])

    assert vectors == [[0.5, 0.5]]
    assert timed.last_vector == [0.5, 0.5]
    assert timed.last_seconds >= 0
    assert (timed.requests, timed.prompt_tokens) == (1, 7)


def test_capture_keeps_only_the_vector_search_statement() -> None:
    capture = vl.QueryCapture()

    capture(None, None, "SELECT 1", {"a": 1}, None, False)
    assert capture.statement is None
    capture(None, None, "SELECT x ORDER BY e <=> %(q)s", {"q": 1}, None, False)
    assert capture.statement is not None and "<=>" in capture.statement
    kept = capture.statement
    capture(None, None, "EXPLAIN (ANALYZE) " + kept, {"q": 2}, None, False)
    assert capture.statement == kept


def test_the_script_refuses_to_run_without_run_api(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert vl.main([]) == 2
    assert "--run-api" in capsys.readouterr().out
