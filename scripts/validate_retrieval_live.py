"""Live validation of production retrieval (Phase 8H).

Runs every Golden Set v2 query through the real :func:`preston.retrieval.
search_knowledge` — live ``text-embedding-3-large`` query embedding, exact
pgvector search over the stored ``halfvec(3072)`` — and measures three things:

* **Quality** — where the expected evidence lands, compared with an offline
  baseline: the *same* strategy (exact search, a pool of 50 chunks, grouped by
  document, 8 documents, 2 chunks each) computed in float32 from the saved
  evaluation vectors. Same method, different vectors, so a difference is
  attributable to the stored precision and the live query embedding.
* **Fidelity** — per case, whether the live (``halfvec``) ranking agrees with
  the float32 replica; every disagreement is listed for review.
* **Latency** — end-to-end time per query, with the query-embedding and
  database parts reported separately, and ``EXPLAIN (ANALYZE, BUFFERS)`` run
  twice on the production query (the first run is a warm-up).

The two ``structured`` cases are checked through ``get_office_locations`` and
kept out of the vector metrics. Nothing is written to the database (every
statement runs in a read-only transaction), ``retrieval.py`` is not modified,
and the reports hold URIs, hashes, ranks, scores and timings — never chunk
text. The only external call is embedding the golden queries, so it needs an
explicit ``--run-api``.

    uv run python scripts/validate_retrieval_live.py --run-api
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import evaluate_chunk_context as cc
import evaluate_embeddings as ev
import evaluate_retrieval as er
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession

from preston.canonical import hash_content
from preston.core.config import Settings, get_settings
from preston.core.db import build_async_engine, build_session_factory
from preston.embed_chunks import (
    DIMENSIONS,
    IDENTITY,
    USD_PER_MILLION_TOKENS,
    MeteredEmbedder,
)
from preston.embedding import OpenAIEmbedder, embedding_identity
from preston.retrieval import (
    CANDIDATE_POOL,
    CHUNKS_PER_DOCUMENT,
    DEFAULT_DOCUMENTS,
    PUBLIC_SCOPES,
    Candidate,
    Evidence,
    get_office_locations,
    group_by_document,
    search_knowledge,
)

DEFAULT_OUTPUT: Final = cc.BASELINE_DIR.parent / "retrieval-live"
#: Approved gate: p95 of end-to-end search (query embedding included).
LATENCY_P95_TARGET_SECONDS: Final = 1.0
#: Cases answered by ``get_office_locations``, not by vector search.
STRUCTURED: Final = "structured"
IMAGE: Final = "image"
RECALL_KS: Final = (1, 3, 5, DEFAULT_DOCUMENTS)
_UUID: Final = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)
_SHORT_HASH: Final = 12


# ---------------------------------------------------------------------------
# Pure measurement helpers
# ---------------------------------------------------------------------------


def percentile(values: Sequence[float], pct: float) -> float:
    """Nearest-rank percentile of ``values``."""
    if not values:
        raise ValueError("No values.")
    ordered = sorted(values)
    return ordered[max(0, math.ceil(pct / 100 * len(ordered)) - 1)]


def distribution(values: Sequence[float]) -> dict[str, float]:
    return {
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "max": max(values),
        "mean": sum(values) / len(values),
    }


def to_candidates(evidence: Sequence[Evidence]) -> list[Candidate]:
    return [
        Candidate(
            e.chunk_content_hash,
            e.canonical_uri,
            e.source_scope,
            e.chunk_index,
            e.score if e.score is not None else 0.0,
        )
        for e in evidence
    ]


def float32_replica(
    query: Sequence[float],
    chunks: Sequence[ev.Chunk],
    vectors: Mapping[str, Sequence[float]],
) -> list[Candidate]:
    """What production search returns, computed in float32 from saved vectors."""
    pool = [
        Candidate(c.content_hash, c.document_uri, c.source_scope, c.chunk_index, score)
        for c, score in ev.rank_chunks(query, chunks, vectors)[:CANDIDATE_POOL]
    ]
    groups = group_by_document(
        pool, documents=DEFAULT_DOCUMENTS, chunks_per_document=CHUNKS_PER_DOCUMENT
    )
    return [candidate for group in groups for candidate in group]


@dataclass(frozen=True, slots=True)
class Fidelity:
    """How closely the live ranking matches the float32 replica for one case."""

    case_id: str
    top1_same_chunk: bool
    top1_same_document: bool
    document_overlap: float
    same_order: bool
    top1_score_delta: float | None

    @property
    def disagrees(self) -> bool:
        """The top chunk differs, or the set of returned documents does."""
        return not self.top1_same_chunk or self.document_overlap < 1.0


def compare(
    case_id: str, live: Sequence[Candidate], offline: Sequence[Candidate]
) -> Fidelity:
    live_docs = list(dict.fromkeys(c.canonical_uri for c in live))
    offline_docs = list(dict.fromkeys(c.canonical_uri for c in offline))
    top1_same_chunk = bool(live and offline) and (
        live[0].chunk_content_hash == offline[0].chunk_content_hash
    )
    union = set(live_docs) | set(offline_docs)
    return Fidelity(
        case_id=case_id,
        top1_same_chunk=top1_same_chunk,
        top1_same_document=bool(live and offline)
        and live[0].canonical_uri == offline[0].canonical_uri,
        document_overlap=len(set(live_docs) & set(offline_docs)) / len(union)
        if union
        else 1.0,
        same_order=[c.chunk_content_hash for c in live]
        == [c.chunk_content_hash for c in offline],
        top1_score_delta=abs(live[0].score - offline[0].score)
        if top1_same_chunk
        else None,
    )


def safety_problems(evidence: Sequence[Evidence]) -> list[str]:
    """Anything in a result a caller must never see; empty means clean."""
    problems: list[str] = []
    for item in evidence:
        if item.source_scope not in PUBLIC_SCOPES:
            problems.append(f"non-public scope {item.source_scope!r}")
        citation = item.citation_uri
        if citation is not None and not citation.startswith("https://"):
            problems.append(f"citation is not a public URL ({item.source_scope})")
        for label, value in (
            ("canonical_uri", item.canonical_uri),
            ("citation_uri", citation),
            ("title", item.title),
        ):
            if value and _UUID.search(value):
                problems.append(f"{label} contains an internal id")
    return problems


def evidence_field_names() -> list[str]:
    """The fields a result exposes, so a test can prove no id is among them."""
    return [f.name for f in fields(Evidence)]


def summarize(outcomes: Sequence[er.Outcome]) -> dict[str, Any]:
    """Hit counts and recall for the answerable outcomes, overall and by category."""
    answerable = [o for o in outcomes if o.answerable]

    def block(members: Sequence[er.Outcome]) -> dict[str, Any]:
        positions = [o.document_position for o in members]
        chunks = [o.chunk_rank for o in members]
        document = ev.metrics(positions, RECALL_KS)
        return {
            "cases": len(members),
            "document_hits": sum(p is not None for p in positions),
            "chunk_hits": sum(r is not None for r in chunks),
            "document_recall_at": {str(k): v for k, v in document.recall_at.items()},
            "document_mrr": document.mrr,
            "chunk_mrr": ev.metrics(chunks, RECALL_KS).mrr,
        }

    return {
        "overall": block(answerable),
        "by_category": {
            category: block([o for o in answerable if o.category == category])
            for category in dict.fromkeys(o.category for o in answerable)
        },
    }


def regressions(
    live: Sequence[er.Outcome], offline: Sequence[er.Outcome]
) -> list[dict[str, Any]]:
    """Cases the offline baseline found that the live search did not."""
    base = {o.case_id: o for o in offline}
    found: list[dict[str, Any]] = []
    for o in live:
        if not o.answerable:
            continue
        reference = base[o.case_id]
        lost_document = (
            reference.document_position is not None and o.document_position is None
        )
        lost_chunk = reference.chunk_rank is not None and o.chunk_rank is None
        if lost_document or lost_chunk:
            found.append(
                {
                    "case_id": o.case_id,
                    "category": o.category,
                    "lost_document": lost_document,
                    "lost_chunk": lost_chunk,
                    "live_document_position": o.document_position,
                    "offline_document_position": reference.document_position,
                    "live_chunk_rank": o.chunk_rank,
                    "offline_chunk_rank": reference.chunk_rank,
                }
            )
    return found


def summarize_plan(root: Mapping[str, Any]) -> dict[str, Any]:
    """The facts worth keeping from one ``EXPLAIN (FORMAT JSON)`` result."""
    nodes: list[str] = []

    def walk(node: Mapping[str, Any]) -> None:
        nodes.append(str(node.get("Node Type")))
        for child in node.get("Plans", []):
            walk(child)

    plan = root["Plan"]
    walk(plan)
    return {
        "planning_ms": root["Planning Time"],
        "execution_ms": root["Execution Time"],
        "nodes": nodes,
        "shared_hit_blocks": plan.get("Shared Hit Blocks"),
        "shared_read_blocks": plan.get("Shared Read Blocks"),
    }


# ---------------------------------------------------------------------------
# Live run
# ---------------------------------------------------------------------------


class TimedEmbedder:
    """Wraps the production embedder to time each call and keep its vector."""

    def __init__(self, inner: MeteredEmbedder) -> None:
        self._inner = inner
        self.last_seconds = 0.0
        self.last_vector: list[float] | None = None

    @property
    def requests(self) -> int:
        return self._inner.requests

    @property
    def prompt_tokens(self) -> int:
        return self._inner.prompt_tokens

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        started = time.perf_counter()
        try:
            vectors = await self._inner.embed(texts)
        finally:
            self.last_seconds = time.perf_counter() - started
        self.last_vector = vectors[0] if vectors else None
        return vectors


class QueryCapture:
    """Remembers the vector-search statement the last search executed."""

    def __init__(self) -> None:
        self.statement: str | None = None
        self.parameters: Any = None

    def __call__(
        self,
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        # Not our own EXPLAIN, which also contains the operator.
        if "<=>" in statement and not statement.startswith("EXPLAIN"):
            self.statement, self.parameters = statement, parameters


async def explain_twice(
    session: AsyncSession, capture: QueryCapture
) -> dict[str, Any] | None:
    """``EXPLAIN (ANALYZE, BUFFERS)`` on the captured production query, twice."""
    if capture.statement is None:
        return None
    connection = await session.connection()
    summaries: list[dict[str, Any]] = []
    for _ in range(2):
        result = await connection.exec_driver_sql(
            "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + capture.statement,
            capture.parameters,
        )
        raw = result.scalar_one()
        data = json.loads(raw) if isinstance(raw, str) else raw
        summaries.append(summarize_plan(data[0]))
    return {"warmup": summaries[0], "measured": summaries[1]}


@dataclass(slots=True)
class LiveCase:
    case_id: str
    evidence: list[Evidence]
    embed_seconds: float
    total_seconds: float
    query_cosine: float
    explain: dict[str, Any] | None

    @property
    def database_seconds(self) -> float:
        return self.total_seconds - self.embed_seconds


@dataclass(slots=True)
class LiveRun:
    cases: list[LiveCase]
    structured: list[Evidence]
    structured_seconds: float
    structured_embed_requests: int
    requests: int
    prompt_tokens: int


async def run_live(
    settings: Settings,
    golden: ev.Golden,
    saved_queries: Mapping[str, Sequence[float]],
) -> LiveRun:
    if settings.database_url is None:
        raise RuntimeError("PRESTON_DATABASE_URL is not configured.")
    timed = TimedEmbedder(OpenAIEmbedder.from_settings(settings))
    engine = build_async_engine(str(settings.database_url))
    capture = QueryCapture()
    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        async with build_session_factory(engine)() as session:
            await session.execute(text("SET TRANSACTION READ ONLY"))
            cases: list[LiveCase] = []
            for case in golden.cases:
                started = time.perf_counter()
                evidence = await search_knowledge(
                    session, timed, case.query, embedding_model=IDENTITY
                )
                total = time.perf_counter() - started
                vector = timed.last_vector
                if vector is None:
                    raise RuntimeError("The query was not embedded.")
                cases.append(
                    LiveCase(
                        case_id=case.id,
                        evidence=evidence,
                        embed_seconds=timed.last_seconds,
                        total_seconds=total,
                        query_cosine=ev.cosine_similarity(
                            vector, saved_queries[hash_content(case.query)]
                        ),
                        explain=await explain_twice(session, capture),
                    )
                )
            requests_before = timed.requests
            started = time.perf_counter()
            structured = await get_office_locations(session)
            structured_seconds = time.perf_counter() - started
            return LiveRun(
                cases=cases,
                structured=structured,
                structured_seconds=structured_seconds,
                structured_embed_requests=timed.requests - requests_before,
                requests=timed.requests,
                prompt_tokens=timed.prompt_tokens,
            )
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _candidate_rows(candidates: Sequence[Candidate]) -> list[dict[str, Any]]:
    return [
        {
            "rank": rank,
            "uri": c.canonical_uri,
            "scope": c.source_scope,
            "chunk": c.chunk_content_hash[:_SHORT_HASH],
            "score": round(c.score, 6),
        }
        for rank, c in enumerate(candidates, start=1)
    ]


def build_report(
    golden: ev.Golden,
    run: LiveRun,
    offline: Mapping[str, list[Candidate]],
    scopes_of: Mapping[str, frozenset[str]],
    *,
    corpus: Mapping[str, int],
) -> dict[str, Any]:
    vector_cases = [c for c in golden.cases if c.category != STRUCTURED]
    live_by_id = {lc.case_id: lc for lc in run.cases}
    live_candidates = {
        cid: to_candidates(lc.evidence) for cid, lc in live_by_id.items()
    }

    live_outcomes = [
        er.outcome(c, live_candidates[c.id], scopes_of[c.id]) for c in vector_cases
    ]
    offline_outcomes = [
        er.outcome(c, offline[c.id], scopes_of[c.id]) for c in vector_cases
    ]
    live_summary, offline_summary = (
        summarize(live_outcomes),
        summarize(offline_outcomes),
    )
    lost = regressions(live_outcomes, offline_outcomes)

    fidelity = [
        compare(c.id, live_candidates[c.id], offline[c.id]) for c in vector_cases
    ]
    disagreements = [f for f in fidelity if f.disagrees]
    outcome_of = {o.case_id: o for o in live_outcomes}
    offline_of = {o.case_id: o for o in offline_outcomes}
    disagreement_rows = [
        {
            "case_id": f.case_id,
            "top1_same_chunk": f.top1_same_chunk,
            "top1_same_document": f.top1_same_document,
            "document_overlap": round(f.document_overlap, 4),
            "live_document_position": outcome_of[f.case_id].document_position,
            "offline_document_position": offline_of[f.case_id].document_position,
            "live_chunk_rank": outcome_of[f.case_id].chunk_rank,
            "offline_chunk_rank": offline_of[f.case_id].chunk_rank,
            "live_top": _candidate_rows(live_candidates[f.case_id][:3]),
            "offline_top": _candidate_rows(offline[f.case_id][:3]),
        }
        for f in disagreements
    ]

    answerable_tops = [
        o.top_score
        for o in live_outcomes
        if o.answerable and o.any_of_rank == 1 and o.top_score is not None
    ]
    unanswerable = {o.case_id: o.top_score for o in live_outcomes if not o.answerable}
    unanswerable_scores = [s for s in unanswerable.values() if s is not None]

    structured_case_ids = [c.id for c in golden.cases if c.category == STRUCTURED]
    returned_hashes = {e.chunk_content_hash for e in run.structured}
    expected_hashes = {
        h
        for c in golden.cases
        if c.category == STRUCTURED
        for h in c.expected_chunk_hashes
    }
    structured_ok = (
        len(run.structured) == 3
        and all(
            e.retrieval_method == "structured"
            and e.source_scope == "office_locations"
            and e.score is None
            for e in run.structured
        )
        and expected_hashes <= returned_hashes
        and run.structured_embed_requests == 0
    )

    problems = [
        f"{lc.case_id}: {p}" for lc in run.cases for p in safety_problems(lc.evidence)
    ] + [f"structured: {p}" for p in safety_problems(run.structured)]

    end_to_end = [lc.total_seconds for lc in run.cases]
    embed = [lc.embed_seconds for lc in run.cases]
    database = [lc.database_seconds for lc in run.cases]
    explained = [lc.explain for lc in run.cases if lc.explain is not None]
    measured = [e["measured"]["execution_ms"] for e in explained]
    warmup = [e["warmup"]["execution_ms"] for e in explained]
    latency: dict[str, Any] = {
        "end_to_end_seconds": distribution(end_to_end),
        "query_embedding_seconds": distribution(embed),
        "database_and_grouping_seconds": distribution(database),
        "explain_execution_ms_measured": distribution(measured) if measured else None,
        "explain_execution_ms_warmup": distribution(warmup) if warmup else None,
        "explain_plan_nodes": sorted(
            {n for e in explained for n in e["measured"]["nodes"]}
        ),
        "explain_cases": len(explained),
    }

    live_all, off_all = live_summary["overall"], offline_summary["overall"]
    live_image = live_summary["by_category"].get(IMAGE)
    off_image = offline_summary["by_category"].get(IMAGE)
    image_ok = (
        live_image is None
        or off_image is None
        or (
            live_image["document_hits"] >= off_image["document_hits"]
            and live_image["chunk_hits"] >= off_image["chunk_hits"]
        )
    )
    cost = run.prompt_tokens * USD_PER_MILLION_TOKENS / 1_000_000
    gates: dict[str, dict[str, Any]] = {
        "quality_document_hit_not_worse": {
            "status": "PASS"
            if live_all["document_hits"] >= off_all["document_hits"]
            else "FAIL",
            "live": live_all["document_hits"],
            "offline": off_all["document_hits"],
            "cases": live_all["cases"],
            "individual_regressions": len(lost),
        },
        "image_no_regression": {
            "status": "PASS" if image_ok else "FAIL",
            "live": live_image,
            "offline": off_image,
        },
        "fidelity_top1": {
            "status": "REVIEW" if disagreements else "PASS",
            "top1_same_chunk": sum(f.top1_same_chunk for f in fidelity),
            "cases": len(fidelity),
            "disagreements": len(disagreements),
        },
        "latency_p95_end_to_end": {
            "status": "PASS"
            if latency["end_to_end_seconds"]["p95"] <= LATENCY_P95_TARGET_SECONDS
            else "FAIL",
            "p95_seconds": latency["end_to_end_seconds"]["p95"],
            "target_seconds": LATENCY_P95_TARGET_SECONDS,
        },
        "safety": {"status": "PASS" if not problems else "FAIL", "problems": problems},
        "structured_office_locations": {
            "status": "PASS" if structured_ok else "FAIL",
            "chunks": len(run.structured),
            "cases": structured_case_ids,
        },
    }

    cases: list[dict[str, Any]] = []
    for c in golden.cases:
        lc = live_by_id[c.id]
        row: dict[str, Any] = {
            "id": c.id,
            "category": c.category,
            "answerable": c.answerable,
            "query": c.query,
            "in_vector_metrics": c.category != STRUCTURED,
            "live": _candidate_rows(live_candidates[c.id]),
            "timing_seconds": {
                "end_to_end": round(lc.total_seconds, 4),
                "query_embedding": round(lc.embed_seconds, 4),
                "database_and_grouping": round(lc.database_seconds, 4),
            },
            "query_cosine_vs_saved": round(lc.query_cosine, 6),
        }
        if c.category != STRUCTURED:
            lo, oo = outcome_of[c.id], offline_of[c.id]
            row["live_outcome"] = {
                "document_position": lo.document_position,
                "chunk_rank": lo.chunk_rank,
                "top_score": lo.top_score,
            }
            row["offline_outcome"] = {
                "document_position": oo.document_position,
                "chunk_rank": oo.chunk_rank,
                "top_score": oo.top_score,
            }
        cases.append(row)

    return {
        "validation_only": True,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "dataset_sha256": golden.sha256,
        "embedding_identity": IDENTITY,
        "corpus": dict(corpus),
        "search": {
            "documents": DEFAULT_DOCUMENTS,
            "chunks_per_document": CHUNKS_PER_DOCUMENT,
            "candidate_pool": CANDIDATE_POOL,
        },
        "embedding_usage": {
            "requests": run.requests,
            "prompt_tokens": run.prompt_tokens,
            "cost_usd": round(cost, 8),
            "cost_basis": f"provider-reported prompt tokens x ${USD_PER_MILLION_TOKENS}/1M",
        },
        "gates": gates,
        "quality": {
            "live": live_summary,
            "offline_float32": offline_summary,
            "regressions": lost,
        },
        "fidelity": {
            "cases": len(fidelity),
            "top1_same_chunk": sum(f.top1_same_chunk for f in fidelity),
            "top1_same_document": sum(f.top1_same_document for f in fidelity),
            "same_order": sum(f.same_order for f in fidelity),
            "mean_document_overlap": sum(f.document_overlap for f in fidelity)
            / len(fidelity),
            "max_top1_score_delta": max(
                (
                    f.top1_score_delta
                    for f in fidelity
                    if f.top1_score_delta is not None
                ),
                default=None,
            ),
            "min_query_cosine_vs_saved": min(lc.query_cosine for lc in run.cases),
            "disagreements": disagreement_rows,
        },
        "unanswerable": {
            "top_scores": unanswerable,
            "max_unanswerable_top_score": max(unanswerable_scores, default=None),
            "min_correct_answerable_top_score": min(answerable_tops, default=None),
            "note": "Measurement only: retrieval makes no answerability decision.",
        },
        "latency": latency,
        "explain_example": explained[0] if explained else None,
        "structured": {
            "chunks": len(run.structured),
            "seconds": round(run.structured_seconds, 4),
            "embedding_requests": run.structured_embed_requests,
            "expected_chunks_returned": expected_hashes <= returned_hashes,
        },
        "cases": cases,
    }


def render_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# Phase 8H — live retrieval validation",
        "",
        (
            f"Generated {report['generated_at']} · `{report['embedding_identity']}` · "
            f"dataset `{report['dataset_sha256'][:12]}`"
        ),
        "",
        "## Gates",
        "",
        "| Gate | Status | Detail |",
        "| --- | --- | --- |",
    ]
    for name, gate in report["gates"].items():
        detail = {k: v for k, v in gate.items() if k != "status"}
        lines.append(
            f"| {name} | **{gate['status']}** | `{json.dumps(detail)[:300]}` |"
        )

    usage = report["embedding_usage"]
    lines += [
        "",
        (
            f"Embedding usage: {usage['requests']} requests, "
            f"{usage['prompt_tokens']:,} prompt tokens, ${usage['cost_usd']} "
            f"({usage['cost_basis']})."
        ),
        "",
        "## Quality (answerable, vector cases)",
        "",
        (
            "| Category | Cases | Doc hits live | Doc hits float32 | "
            "Chunk hits live | Chunk hits float32 |"
        ),
        "| --- | --- | --- | --- | --- | --- |",
    ]
    live, offline = report["quality"]["live"], report["quality"]["offline_float32"]
    rows = {"ALL": (live["overall"], offline["overall"])}
    for category, block in live["by_category"].items():
        rows[category] = (block, offline["by_category"][category])
    for category, (lv, of) in rows.items():
        lines.append(
            f"| {category} | {lv['cases']} | {lv['document_hits']} | "
            f"{of['document_hits']} | {lv['chunk_hits']} | {of['chunk_hits']} |"
        )
    lines += ["", f"Regressions vs float32: {len(report['quality']['regressions'])}"]
    for r in report["quality"]["regressions"]:
        lines.append(f"- `{r['case_id']}` ({r['category']}): {json.dumps(r)}")

    fid = report["fidelity"]
    lines += [
        "",
        "## Fidelity (live halfvec vs float32 replica)",
        "",
        f"- top-1 same chunk: {fid['top1_same_chunk']}/{fid['cases']}",
        f"- top-1 same document: {fid['top1_same_document']}/{fid['cases']}",
        f"- identical ordering: {fid['same_order']}/{fid['cases']}",
        f"- mean document overlap: {fid['mean_document_overlap']:.4f}",
        f"- max |top-1 score delta| (same chunk): {fid['max_top1_score_delta']}",
        f"- min live-vs-saved query cosine: {fid['min_query_cosine_vs_saved']:.6f}",
        f"- disagreements: {len(fid['disagreements'])}",
    ]
    for d in fid["disagreements"]:
        lines.append(f"- `{d['case_id']}`: {json.dumps(d)}")

    un = report["unanswerable"]
    lines += [
        "",
        "## Unanswerable cases (measurement only)",
        "",
        f"- highest unanswerable top score: {un['max_unanswerable_top_score']}",
        f"- lowest correct answerable top score: {un['min_correct_answerable_top_score']}",
    ]

    lat = report["latency"]
    lines += [
        "",
        "## Latency",
        "",
        "| Measure | p50 | p95 | max |",
        "| --- | --- | --- | --- |",
    ]
    for label, key, unit in (
        ("end-to-end (s)", "end_to_end_seconds", ""),
        ("query embedding (s)", "query_embedding_seconds", ""),
        ("database + grouping (s)", "database_and_grouping_seconds", ""),
        ("EXPLAIN execution, measured (ms)", "explain_execution_ms_measured", ""),
        ("EXPLAIN execution, warm-up (ms)", "explain_execution_ms_warmup", ""),
    ):
        d = lat[key]
        if d is not None:
            lines.append(
                f"| {label} | {d['p50']:.3f} | {d['p95']:.3f} | {d['max']:.3f} |{unit}"
            )
    lines.append("")
    lines.append(f"Plan nodes seen: {', '.join(lat['explain_plan_nodes'])}")

    lines += [
        "",
        "## Per case",
        "",
        (
            "| Case | Category | Doc pos live/float32 | Chunk rank live/float32 | "
            "End-to-end (s) |"
        ),
        "| --- | --- | --- | --- | --- |",
    ]
    for c in report["cases"]:
        if "live_outcome" in c:
            lo, oo = c["live_outcome"], c["offline_outcome"]
            lines.append(
                f"| {c['id']} | {c['category']} | "
                f"{lo['document_position']}/{oo['document_position']} | "
                f"{lo['chunk_rank']}/{oo['chunk_rank']} | "
                f"{c['timing_seconds']['end_to_end']} |"
            )
        else:
            lines.append(
                f"| {c['id']} | {c['category']} | (office_locations) | | "
                f"{c['timing_seconds']['end_to_end']} |"
            )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--dataset", type=Path, default=ev.DEFAULT_DATASET)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--run-api",
        action="store_true",
        help="embed the golden queries with the provider (required)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not args.run_api:
        print("Refusing to run: pass --run-api to embed the golden queries.")
        return 2

    settings = get_settings()
    if settings.database_url is None:
        print("PRESTON_DATABASE_URL is not configured.")
        return 1
    if (
        embedding_identity(settings.embedding_model, settings.embedding_dimensions)
        != IDENTITY
    ):
        print("The configured embedding model is not the approved production model.")
        return 1

    golden = ev.load_golden(args.dataset)
    snapshot = ev.load_corpus(
        str(settings.database_url).replace("postgresql+psycopg://", "postgresql://", 1)
    )
    # ``production_embeddings`` is 0 on purpose: the check exists to stop an
    # evaluation running against embedded rows, and here the rows are the subject.
    problems = ev.validate_setup(
        snapshot.chunks,
        golden,
        model=er.MODEL,
        dimensions=DIMENSIONS,
        production_embeddings=0,
    )
    if problems:
        print("SETUP VALIDATION FAILED:", *problems, sep="\n  - ")
        return 1
    if snapshot.production_embeddings != len(snapshot.chunks):
        print(
            f"Only {snapshot.production_embeddings} of {len(snapshot.chunks)} "
            "chunks are embedded; run the backfill first."
        )
        return 1

    chunk_vectors = cc.read_artifact(cc.BASELINE_DIR, DIMENSIONS)
    saved_queries = er.load_query_vectors(golden)
    by_hash = {c.content_hash: c for c in snapshot.chunks}
    scopes_of = {
        case.id: frozenset(by_hash[h].source_scope for h in case.expected_chunk_hashes)
        for case in golden.cases
    }
    offline = {
        case.id: float32_replica(
            saved_queries[hash_content(case.query)], snapshot.chunks, chunk_vectors
        )
        for case in golden.cases
    }

    run = asyncio.run(run_live(settings, golden, saved_queries))
    report = build_report(
        golden,
        run,
        offline,
        scopes_of,
        corpus={
            "chunks": len(snapshot.chunks),
            "embedded": snapshot.production_embeddings,
        },
    )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "report.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    (args.output_dir / "report.md").write_text(
        render_markdown(report), encoding="utf-8"
    )

    for name, gate in report["gates"].items():
        print(f"{gate['status']:6} {name}")
    print(f"Reports written to {args.output_dir}")
    return 1 if any(g["status"] == "FAIL" for g in report["gates"].values()) else 0


if __name__ == "__main__":
    sys.exit(main())
