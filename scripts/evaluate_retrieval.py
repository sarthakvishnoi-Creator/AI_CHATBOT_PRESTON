"""Offline evaluation of the approved retrieval strategy (Phase 8B).

Scores the lane-and-grouping strategy from :mod:`preston.retrieval` against
the golden set, using vectors already saved by the embedding evaluations. It
runs the *same* :func:`~preston.retrieval.select` the search engine will use,
so what is measured here is what will ship.

No API call, no database write, no artifact modified: the corpus is read
through a read-only connection, and the vector artifacts are read as raw
files. Chunk vectors and the first 36 query vectors come from the Large
artifact; the queries added in Golden Set v2 come from the chunk-context
artifact, which embedded them with the same model.

For each case the evaluator reports, within the *returned list*:

* exact supporting chunk — position of the first expected chunk;
* expected document — position of the first chunk from an expected document;
* document position — rank of the first expected document among documents;
* source scope — whether the first result's scope is an expected scope.

Evidence not returned at all counts as a miss, so recall is bounded by what
the quotas return — exactly what a caller would see.

    uv run python scripts/evaluate_retrieval.py
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final

import evaluate_chunk_context as cc
import evaluate_embeddings as ev

from preston.canonical import hash_content
from preston.core.config import get_settings
from preston.retrieval import LANES, Candidate, RetrievalLimits, lane_of, select

MODEL: Final = "text-embedding-3-large"
DIMENSIONS: Final = ev.PROFILES[MODEL]
QUERY_ARTIFACTS: Final = (cc.BASELINE_DIR, cc.DEFAULT_ARTIFACT_DIR)
DEFAULT_OUTPUT: Final = cc.BASELINE_DIR.parent / "retrieval-strategy"
#: Quota grid explored: documents per lane.
INTERCERT_QUOTAS: Final = (3, 5, 8)
BLOG_QUOTAS: Final = (0, 2, 3, 5)
#: The unlaned reference: one pool of 8 documents, the default lanes' budget.
UNLANED: Final = RetrievalLimits(documents_per_lane={"intercert": 8, "blog": 0})


@dataclass(frozen=True, slots=True)
class Outcome:
    case_id: str
    category: str
    answerable: bool
    chunk_rank: int | None
    any_of_rank: int | None
    document_position: int | None
    top_scope_expected: bool | None
    top_score: float | None
    top_lane: str | None
    returned: int


def outcome(
    case: ev.GoldenCase, returned: Sequence[Candidate], scopes: frozenset[str]
) -> Outcome:
    """Where the case's expected evidence sits in one returned list."""
    chunk_rank = any_of_rank = document_position = None
    documents: list[str] = []
    for rank, item in enumerate(returned, start=1):
        if item.canonical_uri not in documents:
            documents.append(item.canonical_uri)
        chunk_hit = item.chunk_content_hash in case.expected_chunk_hashes
        document_hit = item.canonical_uri in case.expected_document_uris
        if chunk_hit and chunk_rank is None:
            chunk_rank = rank
        if (chunk_hit or document_hit) and any_of_rank is None:
            any_of_rank = rank
        if document_hit and document_position is None:
            document_position = len(documents)
    top = returned[0] if returned else None
    return Outcome(
        case_id=case.id,
        category=case.category,
        answerable=case.answerable,
        chunk_rank=chunk_rank,
        any_of_rank=any_of_rank,
        document_position=document_position,
        top_scope_expected=(top.source_scope in scopes)
        if top and case.answerable
        else None,
        top_score=top.score if top else None,
        top_lane=lane_of(top.source_scope) if top else None,
        returned=len(returned),
    )


def lane_candidates(
    query: Sequence[float],
    chunks_by_lane: Mapping[str, Sequence[ev.Chunk]],
    vectors: Mapping[str, Sequence[float]],
    pool: int,
) -> dict[str, list[Candidate]]:
    """Each lane's best ``pool`` chunks, best first — what the vector query returns."""
    return {
        lane: [
            Candidate(
                c.content_hash, c.document_uri, c.source_scope, c.chunk_index, score
            )
            for c, score in ev.rank_chunks(query, chunks, vectors)[:pool]
        ]
        for lane, chunks in chunks_by_lane.items()
    }


def run_strategy(
    golden: ev.Golden,
    candidates: Mapping[str, Mapping[str, list[Candidate]]],
    scopes_of: Mapping[str, frozenset[str]],
    limits: RetrievalLimits,
    lanes_for: Mapping[str, Sequence[str]] | None = None,
) -> list[Outcome]:
    """Apply ``limits`` to every case; ``lanes_for`` restricts a case's lanes."""
    results: list[Outcome] = []
    for case in golden.cases:
        by_lane = candidates[case.id]
        if lanes_for is not None:
            by_lane = {lane: by_lane[lane] for lane in lanes_for[case.id]}
        results.append(outcome(case, select(by_lane, limits), scopes_of[case.id]))
    return results


def summary(outcomes: Sequence[Outcome]) -> dict[str, Any]:
    answerable = [o for o in outcomes if o.answerable]
    by_category: dict[str, Any] = {}
    for category in dict.fromkeys(o.category for o in answerable):
        members = [o for o in answerable if o.category == category]
        by_category[category] = _metrics(
            ev.metrics([o.chunk_rank for o in members]), len(members)
        )
    return {
        "chunk": _metrics(
            ev.metrics([o.chunk_rank for o in answerable]), len(answerable)
        ),
        "document": _metrics(
            ev.metrics([o.any_of_rank for o in answerable]), len(answerable)
        ),
        "document_position": _metrics(
            ev.metrics([o.document_position for o in answerable]), len(answerable)
        ),
        "top_scope_expected": sum(1 for o in answerable if o.top_scope_expected)
        / len(answerable),
        "by_category_chunk": by_category,
    }


def _metrics(m: ev.Metrics, cases: int) -> dict[str, Any]:
    return {
        "cases": cases,
        "recall_at": {str(k): v for k, v in m.recall_at.items()},
        "mrr": m.mrr,
    }


def load_query_vectors(golden: ev.Golden) -> dict[str, Sequence[float]]:
    """Every golden query's vector, from whichever saved artifact holds it."""
    found: dict[str, Sequence[float]] = {}
    for directory in QUERY_ARTIFACTS:
        vectors = cc.read_artifact(directory, DIMENSIONS)
        for case in golden.cases:
            key = hash_content(case.query)
            if key not in found and key in vectors:
                found[key] = vectors[key]
    missing = [c.id for c in golden.cases if hash_content(c.query) not in found]
    if missing:
        raise ValueError(f"No saved query vector for: {missing}")
    return found


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--dataset", type=Path, default=ev.DEFAULT_DATASET)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(list(argv) if argv is not None else None)

    database_url = get_settings().database_url
    if database_url is None:
        print("PRESTON_DATABASE_URL is not configured.")
        return 1
    golden = ev.load_golden(args.dataset)
    snapshot = ev.load_corpus(
        str(database_url).replace("postgresql+psycopg://", "postgresql://", 1)
    )
    problems = ev.validate_setup(
        snapshot.chunks,
        golden,
        model=MODEL,
        dimensions=DIMENSIONS,
        production_embeddings=snapshot.production_embeddings,
    )
    if problems:
        print("SETUP VALIDATION FAILED:", *problems, sep="\n  - ")
        return 1
    chunk_vectors = cc.read_artifact(cc.BASELINE_DIR, DIMENSIONS)
    query_vectors = load_query_vectors(golden)
    chunks_by_lane = {
        lane: [c for c in snapshot.chunks if c.source_scope in scopes]
        for lane, scopes in LANES.items()
    }
    by_hash = {c.content_hash: c for c in snapshot.chunks}
    scopes_of = {
        case.id: frozenset(by_hash[h].source_scope for h in case.expected_chunk_hashes)
        for case in golden.cases
    }
    # The lane holding each answerable case's evidence (for the per-lane view).
    home_lanes = {
        case.id: sorted({lane_of(s) for s in scopes_of[case.id]}) or list(LANES)
        for case in golden.cases
    }

    pool = RetrievalLimits().candidates_per_lane
    candidates = {
        case.id: lane_candidates(
            query_vectors[hash_content(case.query)], chunks_by_lane, chunk_vectors, pool
        )
        for case in golden.cases
    }
    # The unlaned reference: every public chunk in one pool. ``select`` reads
    # quotas by lane name, so the single pool is filed under one lane key.
    flat = {
        case.id: {
            "intercert": [
                Candidate(
                    c.content_hash, c.document_uri, c.source_scope, c.chunk_index, s
                )
                for c, s in ev.rank_chunks(
                    query_vectors[hash_content(case.query)],
                    snapshot.chunks,
                    chunk_vectors,
                )[:pool]
            ]
        }
        for case in golden.cases
    }

    report: dict[str, Any] = {
        "evaluation_only": True,
        "model": MODEL,
        "dataset_sha256": golden.sha256,
        "golden_cases": len(golden.cases),
        "candidates_per_lane": pool,
        "chunks_per_document": 2,
        "strategies": {},
        "cases": {},
    }
    # Unlaned reference: one pool, grouped, 8 documents (the same 16-chunk budget).
    reference = [
        outcome(case, select(flat[case.id], UNLANED), scopes_of[case.id])
        for case in golden.cases
    ]
    report["strategies"]["unlaned (one pool, 8 documents)"] = summary(reference)
    report["cases"]["unlaned (one pool, 8 documents)"] = [asdict(o) for o in reference]
    for intercert in INTERCERT_QUOTAS:
        for blog in BLOG_QUOTAS:
            limits = RetrievalLimits(
                documents_per_lane={"intercert": intercert, "blog": blog}
            )
            name = f"both lanes, intercert={intercert} blog={blog}"
            outcomes = run_strategy(golden, candidates, scopes_of, limits)
            report["strategies"][name] = summary(outcomes)
            report["cases"][name] = [asdict(o) for o in outcomes]
    home = run_strategy(golden, candidates, scopes_of, RetrievalLimits(), home_lanes)
    report["strategies"]["home lane only, default quotas"] = summary(home)
    report["cases"]["home lane only, default quotas"] = [asdict(o) for o in home]

    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "report.json").write_text(
        json.dumps(report, indent=2) + "\n", "utf-8"
    )
    print(
        f"{'strategy':44} {'chunk R@1':>9} {'R@3':>6} {'R@5':>6} {'R@10':>6} {'MRR':>6} | doc R@1 doc MRR | scope@1"
    )
    for name, s in report["strategies"].items():
        c, d = s["chunk"], s["document"]
        print(
            f"{name:44} {c['recall_at']['1']:9.3f} {c['recall_at']['3']:6.3f} {c['recall_at']['5']:6.3f} "
            f"{c['recall_at']['10']:6.3f} {c['mrr']:6.3f} | {d['recall_at']['1']:7.3f} {d['mrr']:7.3f} | {s['top_scope_expected']:.3f}"
        )
    print(f"\nReport written: {args.output_dir / 'report.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
