"""Offline embedding-model evaluation against the golden retrieval set.

Measures how well one embedding model ranks the KB's chunks for the golden
queries. It selects no model and writes nothing to the application database:
vectors live in an evaluation-only artifact directory, never in
``document_chunks.embedding``.

    corpus (read-only) ──┐
                         ├─> embed (Embedder, batched, resumable) ─> artifact
    golden set ──────────┘                                              │
                                  exact cosine ranking <────────────────┘
                                          │
                              Recall@K / MRR + per-case report

**Explicit cost control.** Without ``--run-api`` this makes no OpenAI call:
it validates the setup, reports what would be embedded, and — when the
artifact is already complete — re-computes the evaluation offline from it.
With ``--run-api`` it embeds only what the artifact lacks, so an interrupted
run resumes where it stopped and a finished one costs nothing to repeat.

**The artifact** (``--artifact-dir``) holds ``manifest.json`` (model,
dimensions, running API usage, an evaluation-only marker), ``index.jsonl``
and ``vectors.f32``. Vectors are keyed by the SHA-256 of their text, so a
chunk's key is its own ``content_hash`` and identical texts are embedded
once. Vectors are written before their index lines; a crash can leave a
vector no index line claims, which the next run discards.

**Evidence is any-of.** A retrieved chunk is a hit if its ``content_hash``
is expected *or* its document is expected. The chunk-only and document-only
variants are reported beside it, so the effect of the any-of rule is visible.

Reads the corpus through a read-only connection. Chunk text is held in
memory to be embedded and is never written to the report.

    uv run python scripts/evaluate_embeddings.py             # validate only
    uv run python scripts/evaluate_embeddings.py --run-api   # embed + evaluate
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import sys
from array import array
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast

import psycopg

from preston.canonical import hash_content
from preston.core.config import Settings, get_settings
from preston.embedding import Embedder, EmbeddingError, OpenAIEmbedder

#: The models this evaluation can measure, each at the one dimension it is
#: evaluated at. The dimension follows from the model and is never a separate
#: option, so the two cannot be mismatched from the command line.
PROFILES: Final = {"text-embedding-3-small": 1536, "text-embedding-3-large": 3072}
#: USD per 1M input tokens, standard (non-batch) rate from OpenAI's pricing page.
PRICE_PER_MILLION_TOKENS: Final = {
    "text-embedding-3-small": 0.02,
    "text-embedding-3-large": 0.13,
}
#: The model used when ``--model`` is not given.
MODEL: Final = "text-embedding-3-small"
DIMENSIONS: Final = PROFILES[MODEL]
KS: Final = (1, 3, 5, 10)
TOP_RANKS_REPORTED: Final = 10
UNANSWERABLE_RANKS_REPORTED: Final = 5
DEFAULT_BATCH_SIZE: Final = 128
MAX_BATCH_SIZE: Final = 2048  # OpenAI's per-request input limit.

_REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]
DEFAULT_DATASET: Final = _REPOSITORY_ROOT / "tests/fixtures/golden_retrieval_set.json"


def default_artifact_dir(model: str) -> Path:
    """One artifact directory per model, so evaluations can never share vectors."""
    variant = model.removeprefix("text-embedding-3-")
    return (
        _REPOSITORY_ROOT / f"evaluation_artifacts/embedding-{variant}-{PROFILES[model]}"
    )


DEFAULT_ARTIFACT_DIR: Final = default_artifact_dir(MODEL)

_MANIFEST_NOTE: Final = (
    "EVALUATION ONLY. Vectors in this directory were produced to measure an "
    "embedding model against the golden retrieval set. They are not the KB's "
    "embeddings and must never be copied into document_chunks.embedding."
)
_FLOAT_BYTES: Final = 4


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Chunk:
    content_hash: str
    document_uri: str
    source_scope: str
    chunk_index: int
    # Held to be embedded; excluded from repr so it cannot reach a log.
    content: str = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class CorpusSnapshot:
    chunks: tuple[Chunk, ...]
    production_embeddings: int


@dataclass(frozen=True, slots=True)
class GoldenCase:
    id: str
    category: str
    query: str
    answerable: bool
    expected_document_uris: frozenset[str]
    expected_chunk_hashes: frozenset[str]


@dataclass(frozen=True, slots=True)
class Golden:
    cases: tuple[GoldenCase, ...]
    expected_chunks: int
    sha256: str


@dataclass(frozen=True, slots=True)
class Candidate:
    rank: int
    similarity: float
    content_hash: str
    document_uri: str
    source_scope: str
    chunk_hit: bool
    document_hit: bool

    @property
    def hit(self) -> bool:
        return self.chunk_hit or self.document_hit


@dataclass(frozen=True, slots=True)
class CaseResult:
    case_id: str
    category: str
    query: str
    answerable: bool
    #: First rank (1-based) at which each kind of evidence appears; ``None`` if
    #: it never appears anywhere in the ranking.
    any_of_rank: int | None
    chunk_rank: int | None
    document_rank: int | None
    top1_similarity: float
    top: tuple[Candidate, ...]


@dataclass(frozen=True, slots=True)
class Metrics:
    cases: int
    recall_at: dict[int, float]
    mrr: float
    mrr_at_10: float


# ---------------------------------------------------------------------------
# Golden set and corpus
# ---------------------------------------------------------------------------


def load_golden(path: Path) -> Golden:
    raw_bytes = path.read_bytes()
    raw = cast(dict[str, Any], json.loads(raw_bytes))
    cases = tuple(
        GoldenCase(
            id=str(item["id"]),
            category=str(item["category"]),
            query=str(item["query"]),
            answerable=item["answerable"] is True,
            expected_document_uris=frozenset(
                cast(list[str], item["expected_document_uris"])
            ),
            expected_chunk_hashes=frozenset(
                cast(list[str], item["expected_chunk_hashes"])
            ),
        )
        for item in cast(list[dict[str, Any]], raw["cases"])
    )
    built_against = cast(dict[str, Any], raw["built_against"])
    return Golden(
        cases=cases,
        expected_chunks=int(built_against["chunks"]),
        sha256=hashlib.sha256(raw_bytes).hexdigest(),
    )


def load_corpus(conninfo: str) -> CorpusSnapshot:
    """Read every active chunk through a read-only connection.

    ``embedding`` is only *counted*, to prove it stays unused; no vector is
    read from or written to the application database.
    """
    with psycopg.connect(conninfo, connect_timeout=5) as connection:
        connection.read_only = True
        rows = connection.execute(
            "SELECT c.content_hash, c.content, d.canonical_uri, d.source_scope, "
            "c.chunk_index FROM document_chunks c JOIN documents d "
            "ON d.id = c.document_id WHERE d.status = 'active' "
            "ORDER BY d.canonical_uri, c.chunk_index"
        ).fetchall()
        stored = connection.execute(
            "SELECT count(*) FROM document_chunks WHERE embedding IS NOT NULL"
        ).fetchone()
    chunks = tuple(
        Chunk(
            content_hash=str(content_hash),
            content=str(content),
            document_uri=str(uri),
            source_scope=str(scope),
            chunk_index=int(index),
        )
        for content_hash, content, uri, scope, index in rows
    )
    return CorpusSnapshot(
        chunks=chunks, production_embeddings=int(stored[0]) if stored else 0
    )


def corpus_fingerprint(chunks: Sequence[Chunk]) -> str:
    """One digest of which chunks the evaluation ran against."""
    digest = hashlib.sha256()
    for chunk_hash in sorted(chunk.content_hash for chunk in chunks):
        digest.update(chunk_hash.encode())
    return digest.hexdigest()


def validate_setup(
    chunks: Sequence[Chunk],
    golden: Golden,
    *,
    model: str,
    dimensions: int,
    production_embeddings: int,
) -> list[str]:
    """Return every reason the evaluation must not start; empty means go."""
    problems: list[str] = []
    if model not in PROFILES:
        problems.append(f"model {model!r} is not a supported evaluation model")
    elif dimensions != PROFILES[model]:
        problems.append(
            f"dimensions are {dimensions}, expected {PROFILES[model]} for {model}"
        )
    if len(chunks) != golden.expected_chunks:
        problems.append(
            f"{len(chunks)} active chunks selected, the golden set was built "
            f"against {golden.expected_chunks}"
        )
    empty = sum(1 for chunk in chunks if not chunk.content.strip())
    if empty:
        problems.append(f"{empty} chunk(s) are empty")
    mismatched = sum(
        1 for chunk in chunks if chunk.content_hash != hash_content(chunk.content)
    )
    if mismatched:
        problems.append(
            f"{mismatched} chunk(s) have a content_hash that is not hash_content(content)"
        )
    if production_embeddings:
        problems.append(
            f"{production_embeddings} chunk(s) already carry a production embedding; "
            "the evaluation expects the column to be unused"
        )
    if not golden.cases:
        problems.append("the golden set has no cases")

    uris = {chunk.document_uri for chunk in chunks}
    hashes = {chunk.content_hash for chunk in chunks}
    by_document: dict[str, set[str]] = {}
    for chunk in chunks:
        by_document.setdefault(chunk.document_uri, set()).add(chunk.content_hash)
    for case in golden.cases:
        for uri in sorted(case.expected_document_uris - uris):
            problems.append(
                f"{case.id}: expected document is not an active document: {uri}"
            )
        for expected_hash in sorted(case.expected_chunk_hashes - hashes):
            problems.append(
                f"{case.id}: expected chunk hash is not in the corpus: {expected_hash[:12]}"
            )
        if case.answerable and not case.expected_document_uris:
            problems.append(f"{case.id}: answerable case has no expected document")
        if not case.answerable and (
            case.expected_document_uris or case.expected_chunk_hashes
        ):
            problems.append(f"{case.id}: unanswerable case lists expected evidence")
        owned = {
            content_hash
            for uri in case.expected_document_uris
            for content_hash in by_document.get(uri, set())
        }
        for expected_hash in sorted(case.expected_chunk_hashes - owned):
            problems.append(
                f"{case.id}: chunk {expected_hash[:12]} is not in an expected document"
            )
    return problems


# ---------------------------------------------------------------------------
# Resumable vector artifact
# ---------------------------------------------------------------------------


class VectorStore:
    """An append-only, evaluation-only vector store keyed by text hash."""

    def __init__(self, directory: Path, *, model: str, dimensions: int) -> None:
        self._directory = directory
        self._model = model
        self._dimensions = dimensions
        directory.mkdir(parents=True, exist_ok=True)
        self._manifest_path = directory / "manifest.json"
        self._index_path = directory / "index.jsonl"
        self._vectors_path = directory / "vectors.f32"
        self._manifest = self._open_manifest()
        self._keys = self._repair()

    # -- opening -------------------------------------------------------------

    def _open_manifest(self) -> dict[str, Any]:
        if self._manifest_path.exists():
            manifest = cast(
                dict[str, Any], json.loads(self._manifest_path.read_text("utf-8"))
            )
            if (
                manifest.get("model") != self._model
                or manifest.get("dimensions") != self._dimensions
            ):
                raise ValueError(
                    "The artifact was built for a different model or dimension; "
                    "use a fresh --artifact-dir."
                )
            if manifest.get("byteorder") != sys.byteorder:
                raise ValueError(
                    "The artifact was written on a machine with a different byte order."
                )
            return manifest
        manifest = {
            "evaluation_only": True,
            "note": _MANIFEST_NOTE,
            "model": self._model,
            "dimensions": self._dimensions,
            "vector_format": "float32, one row of `dimensions` values per index line",
            "byteorder": sys.byteorder,
            "created_at": datetime.now(UTC).isoformat(),
            "api_requests": 0,
            "prompt_tokens": 0,
        }
        self._write_manifest(manifest)
        return manifest

    def _repair(self) -> list[str]:
        """Keep only vectors that have a complete index line, and vice versa."""
        keys: list[str] = []
        if self._index_path.exists():
            for line in self._index_path.read_text("utf-8").splitlines():
                try:
                    key = cast(dict[str, str], json.loads(line))["key"]
                except (json.JSONDecodeError, KeyError, TypeError):
                    break
                keys.append(key)
        if len(keys) != len(set(keys)):
            raise ValueError("The artifact index contains a duplicate key.")
        row_bytes = self._dimensions * _FLOAT_BYTES
        stored_rows = (
            self._vectors_path.stat().st_size // row_bytes
            if self._vectors_path.exists()
            else 0
        )
        keys = keys[:stored_rows]
        self._index_path.write_text(
            "".join(json.dumps({"key": key}) + "\n" for key in keys), "utf-8"
        )
        with self._vectors_path.open("ab") as handle:
            handle.truncate(len(keys) * row_bytes)
        return keys

    # -- reading -------------------------------------------------------------

    @property
    def keys(self) -> frozenset[str]:
        return frozenset(self._keys)

    @property
    def api_requests(self) -> int:
        return int(self._manifest["api_requests"])

    @property
    def prompt_tokens(self) -> int:
        return int(self._manifest["prompt_tokens"])

    def load(self) -> dict[str, array[float]]:
        raw: array[float] = array("f")
        with self._vectors_path.open("rb") as handle:
            raw.frombytes(handle.read())
        width = self._dimensions
        return {
            key: raw[row * width : (row + 1) * width]
            for row, key in enumerate(self._keys)
        }

    # -- writing -------------------------------------------------------------

    def append(self, keys: Sequence[str], vectors: Sequence[Sequence[float]]) -> None:
        if len(keys) != len(vectors):
            raise ValueError("Each key needs exactly one vector.")
        if len(set(keys)) != len(keys) or not self.keys.isdisjoint(keys):
            raise ValueError("A vector for this key is already stored.")
        for vector in vectors:
            if len(vector) != self._dimensions or not all(
                math.isfinite(value) for value in vector
            ):
                raise ValueError(
                    "A vector is malformed: wrong dimension or non-finite value."
                )
        block: array[float] = array("f")
        for vector in vectors:
            block.extend(vector)
        with self._vectors_path.open("ab") as handle:
            block.tofile(handle)
            handle.flush()
            os.fsync(handle.fileno())
        with self._index_path.open("a", encoding="utf-8") as handle:
            handle.write("".join(json.dumps({"key": key}) + "\n" for key in keys))
            handle.flush()
            os.fsync(handle.fileno())
        self._keys.extend(keys)

    def add_usage(self, *, requests: int, prompt_tokens: int) -> None:
        self._manifest["api_requests"] = self.api_requests + requests
        self._manifest["prompt_tokens"] = self.prompt_tokens + prompt_tokens
        self._write_manifest(self._manifest)

    def _write_manifest(self, manifest: Mapping[str, Any]) -> None:
        temporary = self._manifest_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(manifest, indent=2) + "\n", "utf-8")
        os.replace(temporary, self._manifest_path)


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EmbedStats:
    embedded: int
    requests: int
    prompt_tokens: int | None  # None when the embedder does not report usage.


def texts_to_embed(chunks: Sequence[Chunk], golden: Golden) -> dict[str, str]:
    """Every text to embed, keyed by its hash — queries first.

    Queries lead, so they travel in the first request: a problem with the
    key, the model or the response shape surfaces before more than one
    batch has been spent.
    """
    texts: dict[str, str] = {}
    for case in golden.cases:
        texts.setdefault(hash_content(case.query), case.query)
    for chunk in chunks:
        texts.setdefault(chunk.content_hash, chunk.content)
    return texts


def _usage(embedder: Embedder) -> tuple[int, int] | None:
    """``(requests, prompt_tokens)`` an embedder has billed so far, if it reports usage."""
    if isinstance(embedder, OpenAIEmbedder):
        return embedder.requests, embedder.prompt_tokens
    return None


async def embed_missing(
    store: VectorStore,
    embedder: Embedder,
    texts: Mapping[str, str],
    *,
    batch_size: int,
    on_batch: Callable[[int, int, int], None] | None = None,
) -> EmbedStats:
    """Embed what the store lacks, one committed batch at a time."""
    missing = [key for key in texts if key not in store.keys]
    batches = [
        missing[start : start + batch_size]
        for start in range(0, len(missing), batch_size)
    ]
    requests = 0
    tokens: int | None = None
    embedded = 0
    for number, keys in enumerate(batches, start=1):
        before = _usage(embedder)
        try:
            vectors = await embedder.embed([texts[key] for key in keys])
        finally:
            # Spend is recorded even when the answer is unusable or the call fails.
            after = _usage(embedder)
            if before is not None and after is not None:
                store.add_usage(
                    requests=after[0] - before[0], prompt_tokens=after[1] - before[1]
                )
                requests += after[0] - before[0]
                tokens = (tokens or 0) + after[1] - before[1]
        if before is None:
            requests += 1
        store.append(keys, vectors)
        embedded += len(keys)
        if on_batch is not None:
            on_batch(number, len(batches), embedded)
    return EmbedStats(embedded=embedded, requests=requests, prompt_tokens=tokens)


def validate_vectors(
    vectors: Mapping[str, Sequence[float]],
    expected_keys: Sequence[str],
    dimensions: int,
) -> list[str]:
    """Return every defect in the stored vectors; empty means complete."""
    problems: list[str] = []
    missing = [key for key in expected_keys if key not in vectors]
    if missing:
        problems.append(f"{len(missing)} text(s) have no vector")
    unexpected = [key for key in vectors if key not in set(expected_keys)]
    if unexpected:
        problems.append(f"{len(unexpected)} vector(s) belong to no expected text")
    wrong = [
        key
        for key in expected_keys
        if key in vectors and len(vectors[key]) != dimensions
    ]
    if wrong:
        problems.append(f"{len(wrong)} vector(s) do not have {dimensions} dimensions")
    non_finite = [
        key
        for key in expected_keys
        if key in vectors and not all(math.isfinite(value) for value in vectors[key])
    ]
    if non_finite:
        problems.append(f"{len(non_finite)} vector(s) contain NaN or infinity")
    zero = [
        key
        for key in expected_keys
        if key in vectors
        and len(vectors[key]) == dimensions
        and math.sumprod(vectors[key], vectors[key]) == 0
    ]
    if zero:
        problems.append(f"{len(zero)} vector(s) are all zeros")
    return problems


# ---------------------------------------------------------------------------
# Ranking and metrics
# ---------------------------------------------------------------------------


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Exact cosine similarity of two vectors of equal length."""
    if len(a) != len(b):
        raise ValueError(f"Vectors differ in length: {len(a)} and {len(b)}.")
    norm_a = math.sqrt(math.sumprod(a, a))
    norm_b = math.sqrt(math.sumprod(b, b))
    if norm_a == 0 or norm_b == 0:
        raise ValueError("Cosine similarity is undefined for a zero vector.")
    return math.sumprod(a, b) / (norm_a * norm_b)


def rank_chunks(
    query: Sequence[float],
    chunks: Sequence[Chunk],
    vectors: Mapping[str, Sequence[float]],
) -> list[tuple[Chunk, float]]:
    """Every chunk with its similarity to ``query``, best first.

    Ties break on ``content_hash`` so the order never depends on input order.
    """
    query_norm = math.sqrt(math.sumprod(query, query))
    if query_norm == 0:
        raise ValueError("Cosine similarity is undefined for a zero vector.")
    norms: dict[str, float] = {}
    scored: list[tuple[Chunk, float]] = []
    for chunk in chunks:
        vector = vectors[chunk.content_hash]
        if len(vector) != len(query):
            raise ValueError(
                f"Vectors differ in length: {len(query)} and {len(vector)}."
            )
        if chunk.content_hash not in norms:
            norms[chunk.content_hash] = math.sqrt(math.sumprod(vector, vector))
        norm = norms[chunk.content_hash]
        if norm == 0:
            raise ValueError("Cosine similarity is undefined for a zero vector.")
        scored.append((chunk, math.sumprod(query, vector) / (query_norm * norm)))
    scored.sort(key=lambda item: (-item[1], item[0].content_hash))
    return scored


def evaluate_case(
    case: GoldenCase,
    ranked: Sequence[tuple[Chunk, float]],
    *,
    top_n: int = TOP_RANKS_REPORTED,
) -> CaseResult:
    """Score one ranking against the case's any-of expected evidence."""
    any_of_rank: int | None = None
    chunk_rank: int | None = None
    document_rank: int | None = None
    top: list[Candidate] = []
    for rank, (chunk, similarity) in enumerate(ranked, start=1):
        chunk_hit = chunk.content_hash in case.expected_chunk_hashes
        document_hit = chunk.document_uri in case.expected_document_uris
        if chunk_hit and chunk_rank is None:
            chunk_rank = rank
        if document_hit and document_rank is None:
            document_rank = rank
        if (chunk_hit or document_hit) and any_of_rank is None:
            any_of_rank = rank
        if rank <= top_n:
            top.append(
                Candidate(
                    rank=rank,
                    similarity=similarity,
                    content_hash=chunk.content_hash,
                    document_uri=chunk.document_uri,
                    source_scope=chunk.source_scope,
                    chunk_hit=chunk_hit,
                    document_hit=document_hit,
                )
            )
        if rank >= top_n and None not in (any_of_rank, chunk_rank, document_rank):
            break
    return CaseResult(
        case_id=case.id,
        category=case.category,
        query=case.query,
        answerable=case.answerable,
        any_of_rank=any_of_rank,
        chunk_rank=chunk_rank,
        document_rank=document_rank,
        top1_similarity=ranked[0][1] if ranked else 0.0,
        top=tuple(top),
    )


def evaluate(
    golden: Golden, chunks: Sequence[Chunk], vectors: Mapping[str, Sequence[float]]
) -> list[CaseResult]:
    results: list[CaseResult] = []
    for case in golden.cases:
        ranked = rank_chunks(vectors[hash_content(case.query)], chunks, vectors)
        results.append(evaluate_case(case, ranked))
    return results


def metrics(ranks: Sequence[int | None], ks: Sequence[int] = KS) -> Metrics:
    """Recall@K and MRR from each case's first-hit rank (``None`` = never)."""
    total = len(ranks)
    if total == 0:
        return Metrics(0, {k: 0.0 for k in ks}, 0.0, 0.0)
    return Metrics(
        cases=total,
        recall_at={
            k: sum(1 for rank in ranks if rank is not None and rank <= k) / total
            for k in ks
        },
        mrr=sum(1 / rank for rank in ranks if rank is not None) / total,
        mrr_at_10=sum(1 / rank for rank in ranks if rank is not None and rank <= 10)
        / total,
    )


def variant_metrics(results: Sequence[CaseResult]) -> dict[str, Metrics]:
    answerable = [result for result in results if result.answerable]
    return {
        "any_of": metrics([result.any_of_rank for result in answerable]),
        "chunk_exact": metrics([result.chunk_rank for result in answerable]),
        "document": metrics([result.document_rank for result in answerable]),
    }


def category_metrics(results: Sequence[CaseResult]) -> dict[str, Metrics]:
    categories = dict.fromkeys(
        result.category for result in results if result.answerable
    )
    return {
        category: metrics(
            [r.any_of_rank for r in results if r.answerable and r.category == category]
        )
        for category in categories
    }


def weakest_correct_top1(results: Sequence[CaseResult]) -> float | None:
    """The lowest top-1 similarity among answerable cases whose top result was a hit.

    A data-derived reference for "strong": the weakest score at which the
    model still returned correct evidence first. ``None`` if no case did.
    """
    scores = [r.top1_similarity for r in results if r.answerable and r.any_of_rank == 1]
    return min(scores) if scores else None


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def build_report(
    results: Sequence[CaseResult],
    *,
    model: str,
    dimensions: int,
    chunks_embedded: int,
    queries: int,
    api_requests: int,
    prompt_tokens: int,
    run_requests: int,
    run_prompt_tokens: int | None,
    dataset_sha256: str,
    corpus_sha256: str,
    integrity: Mapping[str, Any],
    errors: Sequence[str],
) -> dict[str, Any]:
    answerable = [r for r in results if r.answerable]
    threshold = weakest_correct_top1(results)
    unanswerable = [
        {
            "case_id": r.case_id,
            "query": r.query,
            "top1_similarity": r.top1_similarity,
            "strong_candidate": None
            if threshold is None
            else r.top1_similarity >= threshold,
            "top": [asdict(c) for c in r.top[:UNANSWERABLE_RANKS_REPORTED]],
        }
        for r in results
        if not r.answerable
    ]
    return {
        "evaluation_only": True,
        "model": model,
        "dimensions": dimensions,
        "chunks_embedded": chunks_embedded,
        "queries": queries,
        "api_requests_total": api_requests,
        "prompt_tokens_total": prompt_tokens,
        "api_requests_this_run": run_requests,
        "prompt_tokens_this_run": run_prompt_tokens,
        "price_per_million_tokens_usd": PRICE_PER_MILLION_TOKENS.get(model),
        "estimated_cost_usd": (
            None
            if model not in PRICE_PER_MILLION_TOKENS
            else prompt_tokens * PRICE_PER_MILLION_TOKENS[model] / 1_000_000
        ),
        "dataset_sha256": dataset_sha256,
        "corpus_sha256": corpus_sha256,
        "ks": list(KS),
        "metrics": {
            name: _metrics_dict(m) for name, m in variant_metrics(results).items()
        },
        "metrics_by_category": {
            name: _metrics_dict(m) for name, m in category_metrics(results).items()
        },
        "answerable_cases": [
            {
                "case_id": r.case_id,
                "category": r.category,
                "query": r.query,
                "any_of_rank": r.any_of_rank,
                "chunk_rank": r.chunk_rank,
                "document_rank": r.document_rank,
                "top1_similarity": r.top1_similarity,
                "top": [asdict(c) | {"hit": c.hit} for c in r.top],
            }
            for r in answerable
        ],
        "misses": [
            r.case_id for r in answerable if r.any_of_rank is None or r.any_of_rank > 10
        ],
        "found_only_below_rank_1": [
            {"case_id": r.case_id, "any_of_rank": r.any_of_rank}
            for r in answerable
            if r.any_of_rank is not None and r.any_of_rank > 1
        ],
        "unanswerable_cases": unanswerable,
        "unanswerable_strong_threshold": threshold,
        "integrity": dict(integrity),
        "errors": list(errors),
    }


def _metrics_dict(m: Metrics) -> dict[str, Any]:
    return {
        "cases": m.cases,
        "recall_at": {str(k): v for k, v in m.recall_at.items()},
        "mrr": m.mrr,
        "mrr_at_10": m.mrr_at_10,
    }


def render_markdown(report: Mapping[str, Any]) -> str:
    """A concise human summary; carries no chunk text."""
    lines = [
        "# Embedding evaluation (evaluation only)",
        "",
        f"- Model: `{report['model']}`, {report['dimensions']} dimensions",
        f"- Chunks embedded: {report['chunks_embedded']}; queries: {report['queries']}",
        (
            f"- API requests: {report['api_requests_total']} total "
            f"({report['api_requests_this_run']} this run)"
        ),
        (
            f"- Prompt tokens: {report['prompt_tokens_total']} total "
            f"({_count(report['prompt_tokens_this_run'])} this run)"
        ),
        (
            "- Estimated cost: "
            f"{_cost(report.get('estimated_cost_usd'), report.get('price_per_million_tokens_usd'))}"
        ),
        (
            f"- Dataset sha256: `{str(report['dataset_sha256'])[:16]}…`; "
            f"corpus sha256: `{str(report['corpus_sha256'])[:16]}…`"
        ),
        "",
        "## Metrics (answerable cases)",
        "",
        "| Evidence rule | Cases | "
        + " | ".join(f"Recall@{k}" for k in report["ks"])
        + " | MRR |",
        "| --- | --- | " + " | ".join("---" for _ in report["ks"]) + " | --- |",
    ]
    for name, label in (
        ("any_of", "any-of (primary)"),
        ("chunk_exact", "chunk only"),
        ("document", "document only"),
    ):
        m = report["metrics"][name]
        recalls = " | ".join(f"{m['recall_at'][str(k)]:.3f}" for k in report["ks"])
        lines.append(f"| {label} | {m['cases']} | {recalls} | {m['mrr']:.3f} |")

    lines += [
        "",
        "### By category (any-of)",
        "",
        "| Category | Cases | "
        + " | ".join(f"R@{k}" for k in report["ks"])
        + " | MRR |",
        "| --- | --- | " + " | ".join("---" for _ in report["ks"]) + " | --- |",
    ]
    for category, m in report["metrics_by_category"].items():
        recalls = " | ".join(f"{m['recall_at'][str(k)]:.3f}" for k in report["ks"])
        lines.append(f"| {category} | {m['cases']} | {recalls} | {m['mrr']:.3f} |")

    lines += [
        "",
        "## Answerable cases",
        "",
        "| Case | Category | Any-of rank | Chunk rank | Document rank | Top-1 similarity |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for case in report["answerable_cases"]:
        lines.append(
            f"| {case['case_id']} | {case['category']} | {_rank(case['any_of_rank'])} | "
            f"{_rank(case['chunk_rank'])} | {_rank(case['document_rank'])} | {case['top1_similarity']:.3f} |"
        )
    lines += [
        "",
        f"- Not found within the top 10: {', '.join(report['misses']) or 'none'}",
        "- Found only below rank 1: "
        + (
            ", ".join(
                f"{c['case_id']} (rank {c['any_of_rank']})"
                for c in report["found_only_below_rank_1"]
            )
            or "none"
        ),
        "",
        "## Unanswerable cases",
        "",
        (
            "Reference for 'strong': the weakest top-1 similarity at which an "
            "answerable case still returned correct evidence first = "
            f"{_score(report['unanswerable_strong_threshold'])}."
        ),
        "",
        "| Case | Top-1 similarity | At or above reference | Top-1 document |",
        "| --- | --- | --- | --- |",
    ]
    for case in report["unanswerable_cases"]:
        top_document = case["top"][0]["document_uri"] if case["top"] else "-"
        lines.append(
            f"| {case['case_id']} | {case['top1_similarity']:.3f} | "
            f"{_flag(case['strong_candidate'])} | {top_document} |"
        )
    lines += ["", "## Integrity", ""]
    lines += [f"- {key}: {value}" for key, value in report["integrity"].items()]
    lines += ["", "## Errors", ""] + (
        [f"- {error}" for error in report["errors"]] or ["- none"]
    )
    return "\n".join(lines) + "\n"


def _cost(cost: float | None, price: float | None) -> str:
    if cost is None or price is None:
        return "n/a"
    return f"${cost:.4f} (tokens x ${price:.2f} per 1M, standard list price)"


def _count(value: int | None) -> str:
    return "n/a" if value is None else str(value)


def _rank(rank: int | None) -> str:
    return "-" if rank is None else str(rank)


def _score(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def _flag(value: bool | None) -> str:
    return "n/a" if value is None else ("yes" if value else "no")


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def _default_corpus_loader() -> CorpusSnapshot:
    database_url = get_settings().database_url
    if database_url is None:
        raise ValueError("PRESTON_DATABASE_URL is not configured.")
    return load_corpus(
        str(database_url).replace("postgresql+psycopg://", "postgresql://", 1)
    )


def embedder_for(model: str, settings: Settings) -> Embedder:
    """The evaluation embedder: pinned model and dimensions, key from settings.

    Only the API key is taken from configuration, so a production setting
    can never change what this evaluation measures.
    """
    pinned = settings.model_copy(
        update={"embedding_model": model, "embedding_dimensions": PROFILES[model]}
    )
    return OpenAIEmbedder.from_settings(pinned)


def _progress(batch: int, batches: int, embedded: int) -> None:
    print(f"  batch {batch}/{batches} stored ({embedded} embedded so far)", flush=True)


def main(
    argv: Sequence[str] | None = None,
    *,
    corpus_loader: Callable[[], CorpusSnapshot] = _default_corpus_loader,
    embedder_factory: Callable[[], Embedder] | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0] if __doc__ else None
    )
    parser.add_argument(
        "--run-api",
        action="store_true",
        help="Make real OpenAI calls (otherwise none are made).",
    )
    parser.add_argument(
        "--model",
        choices=sorted(PROFILES),
        default=MODEL,
        help="The model to evaluate; its dimension is fixed by the model. "
        "Settings (.env) never choose it.",
    )
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        default=None,
        help="Defaults to a directory named for the model.",
    )
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not 1 <= args.batch_size <= MAX_BATCH_SIZE:
        parser.error(f"--batch-size must be between 1 and {MAX_BATCH_SIZE}")
    args.dimensions = PROFILES[args.model]
    if args.artifact_dir is None:
        args.artifact_dir = default_artifact_dir(args.model)
    selected_model: str = args.model

    def configured_embedder() -> Embedder:
        return embedder_for(selected_model, get_settings())

    return asyncio.run(
        _run(args, corpus_loader, embedder_factory or configured_embedder)
    )


async def _run(
    args: argparse.Namespace,
    corpus_loader: Callable[[], CorpusSnapshot],
    embedder_factory: Callable[[], Embedder],
) -> int:
    model: str = args.model
    dimensions: int = args.dimensions
    golden = load_golden(args.dataset)
    snapshot = corpus_loader()
    chunks = snapshot.chunks

    problems = validate_setup(
        chunks,
        golden,
        model=model,
        dimensions=dimensions,
        production_embeddings=snapshot.production_embeddings,
    )
    store = VectorStore(args.artifact_dir, model=model, dimensions=dimensions)
    texts = texts_to_embed(chunks, golden)
    missing = [key for key in texts if key not in store.keys]
    query_keys = {hash_content(case.query) for case in golden.cases}
    missing_chunks = sum(1 for key in missing if key not in query_keys)
    missing_queries = len(missing) - missing_chunks

    print(f"Model: {model} ({dimensions} dimensions); artifact: {args.artifact_dir}")
    print(f"Active chunks selected: {len(chunks)}; golden queries: {len(golden.cases)}")
    print(f"Expected total vectors: {len(texts)}")
    print(
        f"Still to embed: {missing_chunks} chunks + {missing_queries} queries "
        f"in {math.ceil(len(missing) / args.batch_size)} request(s) of up to {args.batch_size}"
    )
    print(
        f"Estimated tokens to embed (characters / 4): {sum(len(texts[k]) for k in missing) // 4}"
    )
    print(f"Production embeddings present: {snapshot.production_embeddings}")
    if problems:
        print("SETUP VALIDATION FAILED — no API call was made:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("Setup validation passed.")

    errors: list[str] = []
    stats = EmbedStats(0, 0, 0)
    if missing and not args.run_api:
        print("No API call made (re-run with --run-api to embed what is missing).")
        return 0
    if missing:
        try:
            embedder = embedder_factory()
            stats = await embed_missing(
                store, embedder, texts, batch_size=args.batch_size, on_batch=_progress
            )
        except Exception as error:  # noqa: BLE001 - report the class only; SDK text can quote a key
            print(
                f"EMBEDDING STOPPED: {type(error).__name__}. Progress is saved; re-run with --run-api to resume."
            )
            if isinstance(error, EmbeddingError):
                print(f"  {error.detail}")
            return 1

    vectors = store.load()
    defects = validate_vectors(vectors, list(texts), dimensions)
    if defects:
        print("VECTOR VALIDATION FAILED:")
        for defect in defects:
            print(f"  - {defect}")
        return 1
    print(
        f"Vectors validated: {len(texts)} texts, each with exactly one {dimensions}-dimensional vector."
    )

    results = evaluate(golden, chunks, vectors)
    after = corpus_loader()
    integrity = {
        "production_embeddings_before": snapshot.production_embeddings,
        "production_embeddings_after": after.production_embeddings,
        "corpus_unchanged_during_run": corpus_fingerprint(after.chunks)
        == corpus_fingerprint(chunks),
        "vectors_validated": len(texts),
    }
    report = build_report(
        results,
        model=model,
        dimensions=dimensions,
        chunks_embedded=sum(1 for key in texts if key not in query_keys),
        queries=len(query_keys),
        api_requests=store.api_requests,
        prompt_tokens=store.prompt_tokens,
        run_requests=stats.requests,
        run_prompt_tokens=stats.prompt_tokens,
        dataset_sha256=golden.sha256,
        corpus_sha256=corpus_fingerprint(chunks),
        integrity=integrity,
        errors=errors,
    )
    (args.artifact_dir / "report.json").write_text(
        json.dumps(report, indent=2) + "\n", "utf-8"
    )
    markdown = render_markdown(report)
    (args.artifact_dir / "report.md").write_text(markdown, "utf-8")
    print()
    print(markdown)
    return (
        0
        if all(
            (
                integrity["corpus_unchanged_during_run"],
                integrity["production_embeddings_after"]
                == integrity["production_embeddings_before"],
            )
        )
        else 1
    )


if __name__ == "__main__":
    sys.exit(main())
