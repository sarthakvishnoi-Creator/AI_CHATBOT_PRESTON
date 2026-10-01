"""Offline experiment: does adding context to each chunk's embedding input help?

Compares two representations of the same 6,973 stored chunks, embedded with
the approved model (``text-embedding-3-large``, 3,072 dimensions):

    current     the chunk content, exactly as stored
    contextual  {document title} › {heading path}  +  blank line  +  content

**Nothing in production changes.** Chunks, their ``content_hash``, the
chunker and ``document_chunks.embedding`` are untouched; the contextual text
exists only as embedding input inside this experiment's own artifact.

**The heading path is reconstructed, not invented.** Stored chunks do not
record their headings, so :func:`contextual_chunks` replays the production
chunking over each document's stored blocks while tracking the heading in
force at each chunk's first character, and the run aborts unless every
replayed chunk equals the stored chunk text byte for byte.

**Reuse.** The current representation's vectors come from the completed
Large artifact, read as raw files and never opened for writing. The new
artifact holds only what that one lacks: one vector per contextual chunk and
one per golden query the Large run never saw. Both representations are then
ranked with the *same* query vectors, so the comparison isolates the chunk
representation.

    uv run python scripts/evaluate_chunk_context.py             # validate only
    uv run python scripts/evaluate_chunk_context.py --run-api   # embed + compare
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from array import array
from bisect import bisect_right
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast

import evaluate_embeddings as ev
import psycopg

from preston.canonical import (
    Block,
    CanonicalDocument,
    FaqPair,
    Heading,
    ImageRef,
    Provenance,
    block_text,
    blocks_from_json,
    hash_content,
)
from preston.core.config import get_settings
from preston.embedding import Embedder
from preston.ingestion import (
    CHUNK_OVERLAP,
    CHUNK_SIZE,
    NON_KNOWLEDGE_ALT_TEXT,
    build_chunks,
    chunk_text,
)

MODEL: Final = "text-embedding-3-large"
DIMENSIONS: Final = ev.PROFILES[MODEL]
SEPARATOR: Final = " › "
BASELINE_DIR: Final = ev.default_artifact_dir(MODEL)
DEFAULT_ARTIFACT_DIR: Final = BASELINE_DIR.with_name(BASELINE_DIR.name + "-context")
FOCUS_CASES: Final = (
    "exact-02",
    "image-03",
    "image-05",
    "cross-01",
    "cross-02",
    "cross-03",
    "paraphrase-08",
    "paraphrase-09",
)


# ---------------------------------------------------------------------------
# Contextual representation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ContextualChunk:
    content: str
    heading_path: tuple[str, ...]


def _rendered(block: Block) -> str:
    """The text ``build_chunks`` takes from a block (mirrors ``_chunk_text``)."""

    def non_knowledge(item: Block) -> bool:
        return (
            isinstance(item, ImageRef)
            and " ".join(item.alt.split()).casefold() in NON_KNOWLEDGE_ALT_TEXT
        )

    if non_knowledge(block):
        return ""
    if isinstance(block, FaqPair):
        return block_text(
            replace(
                block, answer=tuple(a for a in block.answer if not non_knowledge(a))
            )
        )
    return block_text(block)


def contextual_chunks(document: CanonicalDocument) -> list[ContextualChunk]:
    """Replay ``build_chunks``, recording the heading path at each chunk's start.

    A heading opens a section at its own level and closes deeper ones. A
    windowed chunk takes the path in force at its first character; an FAQ
    pair, which is never windowed, takes the path in force where it sits.
    """
    step = CHUNK_SIZE - CHUNK_OVERLAP
    stack: list[tuple[int, str]] = []
    out: list[ContextualChunk] = []
    run: list[str] = []
    starts: list[int] = []  # offset of each run block in the joined run text
    paths: list[tuple[str, ...]] = []  # heading path in force from that block on

    def path() -> tuple[str, ...]:
        return tuple(text for _, text in stack)

    def flush() -> None:
        if run:
            for index, text in enumerate(chunk_text("\n\n".join(run))):
                # starts[0] is 0, so every window start falls in some block.
                position = bisect_right(starts, index * step) - 1
                out.append(ContextualChunk(text, paths[position]))
            run.clear()
            starts.clear()
            paths.clear()

    for block in document.blocks:
        rendered = _rendered(block)
        if isinstance(block, FaqPair):
            flush()
            if rendered:
                out.append(ContextualChunk(rendered, path()))
            continue
        if isinstance(block, Heading) and rendered:
            while stack and stack[-1][0] >= block.level:
                stack.pop()
            stack.append((block.level, block.text))
        if rendered:
            starts.append(sum(len(part) + 2 for part in run))
            run.append(rendered)
            paths.append(path())
    flush()
    return out


def contextual_text(title: str, chunk: ContextualChunk) -> str:
    """``{title} › {heading path}`` then a blank line, then the chunk unchanged.

    Headings that merely repeat the title are dropped, so a page whose first
    heading is its own title does not say it twice.
    """
    key = title.casefold().strip()
    headings = [h for h in chunk.heading_path if h.casefold().strip() != key]
    return SEPARATOR.join([title, *headings]) + "\n\n" + chunk.content


# ---------------------------------------------------------------------------
# Corpus
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Corpus:
    chunks: tuple[ev.Chunk, ...]
    #: content_hash -> contextual embedding input.
    contextual: dict[str, str]
    production_embeddings: int


def load_corpus(conninfo: str) -> Corpus:
    """Read documents and chunks read-only; build and verify the contextual inputs."""
    snapshot = ev.load_corpus(conninfo)
    with psycopg.connect(conninfo, connect_timeout=5) as connection:
        connection.read_only = True
        documents = connection.execute(
            "SELECT canonical_uri, title, blocks FROM documents WHERE status = 'active'"
        ).fetchall()
    stored: dict[str, list[ev.Chunk]] = {}
    for chunk in snapshot.chunks:
        stored.setdefault(chunk.document_uri, []).append(chunk)
    contextual: dict[str, str] = {}
    mismatched: list[str] = []
    for uri, title, blocks in documents:
        document = CanonicalDocument(
            canonical_uri=str(uri),
            source_type="mysql",
            content_type="blog",
            source_scope="x",
            title=str(title),
            blocks=blocks_from_json(blocks),
            provenance=Provenance(
                retrieved_at=datetime.now(UTC),
                extractor_version=1,
                normalizer_version=1,
            ),
        )
        replayed = contextual_chunks(document)
        own = sorted(stored.get(str(uri), []), key=lambda c: c.chunk_index)
        if [c.content for c in replayed] != [c.content for c in own] or [
            c.content for c in replayed
        ] != build_chunks(document):
            mismatched.append(str(uri))
            continue
        for chunk, context in zip(own, replayed, strict=True):
            contextual[chunk.content_hash] = contextual_text(str(title), context)
    if mismatched:
        raise ValueError(
            f"Replayed chunking differs from stored chunks for {len(mismatched)} "
            "document(s); the contextual representation would not describe the corpus."
        )
    return Corpus(snapshot.chunks, contextual, snapshot.production_embeddings)


# ---------------------------------------------------------------------------
# Vectors
# ---------------------------------------------------------------------------


def read_artifact(directory: Path, dimensions: int) -> dict[str, array[float]]:
    """Read an artifact's vectors without opening it for writing."""
    manifest = cast(
        dict[str, Any], json.loads((directory / "manifest.json").read_text("utf-8"))
    )
    if (manifest["model"], manifest["dimensions"]) != (MODEL, dimensions):
        raise ValueError(f"{directory} is not a {MODEL} / {dimensions} artifact.")
    keys = [
        cast(dict[str, str], json.loads(line))["key"]
        for line in (directory / "index.jsonl").read_text("utf-8").splitlines()
    ]
    raw: array[float] = array("f")
    raw.frombytes((directory / "vectors.f32").read_bytes())
    if len(raw) != len(keys) * dimensions:
        raise ValueError(f"{directory} has a truncated vector file.")
    return {
        key: raw[row * dimensions : (row + 1) * dimensions]
        for row, key in enumerate(keys)
    }


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def score(
    golden: ev.Golden,
    chunks: Sequence[ev.Chunk],
    chunk_vectors: Mapping[str, Sequence[float]],
    query_vectors: Mapping[str, Sequence[float]],
) -> list[ev.CaseResult]:
    """Rank every chunk for every case; chunk vectors keyed by content_hash."""
    return [
        ev.evaluate_case(
            case,
            ev.rank_chunks(
                query_vectors[hash_content(case.query)], chunks, chunk_vectors
            ),
        )
        for case in golden.cases
    ]


def summarize(
    results: Sequence[ev.CaseResult], case_ids: set[str] | None = None
) -> dict[str, Any]:
    selected = [
        r
        for r in results
        if r.answerable and (case_ids is None or r.case_id in case_ids)
    ]
    return {
        "any_of": ev.metrics([r.any_of_rank for r in selected]),
        "chunk_exact": ev.metrics([r.chunk_rank for r in selected]),
        "by_category": ev.category_metrics(selected),
    }


def changes(
    current: Sequence[ev.CaseResult], contextual: Sequence[ev.CaseResult]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for before, after in zip(current, contextual, strict=True):
        rows.append(
            {
                "case_id": before.case_id,
                "category": before.category,
                "answerable": before.answerable,
                "current_any_of_rank": before.any_of_rank,
                "contextual_any_of_rank": after.any_of_rank,
                "current_chunk_rank": before.chunk_rank,
                "contextual_chunk_rank": after.chunk_rank,
                "current_top1": before.top1_similarity,
                "contextual_top1": after.top1_similarity,
                "contextual_top3": [
                    {
                        "rank": c.rank,
                        "scope": c.source_scope,
                        "document_uri": c.document_uri,
                        "hit": c.hit,
                    }
                    for c in after.top[:3]
                ],
            }
        )
    return rows


def _metrics_json(m: ev.Metrics) -> dict[str, Any]:
    return {
        "cases": m.cases,
        "recall_at": {str(k): v for k, v in m.recall_at.items()},
        "mrr": m.mrr,
    }


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def _default_corpus() -> Corpus:
    database_url = get_settings().database_url
    if database_url is None:
        raise ValueError("PRESTON_DATABASE_URL is not configured.")
    return load_corpus(
        str(database_url).replace("postgresql+psycopg://", "postgresql://", 1)
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    corpus_loader: Callable[[], Corpus] = _default_corpus,
    embedder_factory: Callable[[], Embedder] | None = None,
    baseline_dir: Path = BASELINE_DIR,
) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument(
        "--run-api", action="store_true", help="Make real OpenAI calls."
    )
    parser.add_argument("--dataset", type=Path, default=ev.DEFAULT_DATASET)
    parser.add_argument("--artifact-dir", type=Path, default=DEFAULT_ARTIFACT_DIR)
    parser.add_argument("--batch-size", type=int, default=ev.DEFAULT_BATCH_SIZE)
    args = parser.parse_args(list(argv) if argv is not None else None)

    def configured() -> Embedder:
        return ev.embedder_for(MODEL, get_settings())

    return asyncio.run(
        _run(args, corpus_loader, embedder_factory or configured, baseline_dir)
    )


async def _run(
    args: argparse.Namespace,
    corpus_loader: Callable[[], Corpus],
    embedder_factory: Callable[[], Embedder],
    baseline_dir: Path,
) -> int:
    golden = ev.load_golden(args.dataset)
    corpus = corpus_loader()
    baseline = read_artifact(baseline_dir, DIMENSIONS)

    problems = ev.validate_setup(
        corpus.chunks,
        golden,
        model=MODEL,
        dimensions=DIMENSIONS,
        production_embeddings=corpus.production_embeddings,
    )
    missing_baseline = [
        c.content_hash for c in corpus.chunks if c.content_hash not in baseline
    ]
    if missing_baseline:
        problems.append(
            f"{len(missing_baseline)} chunk(s) have no vector in the baseline artifact"
        )
    if len(corpus.contextual) != len(corpus.chunks):
        problems.append("not every chunk has a contextual representation")

    queries = {hash_content(case.query): case.query for case in golden.cases}
    new_queries = {key: text for key, text in queries.items() if key not in baseline}
    texts: dict[str, str] = dict(new_queries)
    for text in corpus.contextual.values():
        texts.setdefault(hash_content(text), text)

    store = ev.VectorStore(args.artifact_dir, model=MODEL, dimensions=DIMENSIONS)
    missing = [key for key in texts if key not in store.keys]
    print(f"Model: {MODEL} ({DIMENSIONS} dimensions); artifact: {args.artifact_dir}")
    print(f"Baseline (current representation) read from: {baseline_dir}")
    print(
        f"Chunks: {len(corpus.chunks)}; contextual inputs: {len(corpus.contextual)} (all replayed chunks match stored chunks)"
    )
    print(
        f"Golden cases: {len(golden.cases)}; queries reused from baseline: {len(queries) - len(new_queries)}; new: {len(new_queries)}"
    )
    print(
        f"Still to embed: {len(missing)} texts in {-(-len(missing) // args.batch_size)} request(s)"
    )
    print(
        f"Estimated tokens (characters / 4): {sum(len(texts[k]) for k in missing) // 4}"
    )
    if problems:
        print("SETUP VALIDATION FAILED — no API call was made:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("Setup validation passed.")
    if missing and not args.run_api:
        print("No API call made (re-run with --run-api to embed what is missing).")
        return 0
    if missing:
        try:
            await ev.embed_missing(
                store, embedder_factory(), texts, batch_size=args.batch_size
            )
        except Exception as error:  # noqa: BLE001 - class only; SDK text can quote a key
            print(
                f"EMBEDDING STOPPED: {type(error).__name__}. Progress is saved; re-run to resume."
            )
            return 1

    experiment = store.load()
    defects = ev.validate_vectors(experiment, list(texts), DIMENSIONS)
    if defects:
        print("VECTOR VALIDATION FAILED:", *defects, sep="\n  - ")
        return 1
    query_vectors = {key: baseline.get(key) or experiment[key] for key in queries}
    contextual_vectors = {
        content_hash: experiment[hash_content(text)]
        for content_hash, text in corpus.contextual.items()
    }
    current = score(golden, corpus.chunks, baseline, query_vectors)
    contextual = score(golden, corpus.chunks, contextual_vectors, query_vectors)

    v1 = {case.id for case in golden.cases[:36]}
    report: dict[str, Any] = {
        "evaluation_only": True,
        "model": MODEL,
        "dimensions": DIMENSIONS,
        "representation": "{title} › {heading path}\\n\\n{chunk content}",
        "chunks": len(corpus.chunks),
        "golden_cases": len(golden.cases),
        "dataset_sha256": golden.sha256,
        "api_requests_total": store.api_requests,
        "prompt_tokens_total": store.prompt_tokens,
        "estimated_cost_usd": store.prompt_tokens
        * ev.PRICE_PER_MILLION_TOKENS[MODEL]
        / 1_000_000,
        "metrics": {},
        "cases": changes(current, contextual),
    }
    for label, subset in (("all", None), ("v1_cases", v1)):
        for name, results in (("current", current), ("contextual", contextual)):
            summary = summarize(results, subset)
            report["metrics"][f"{label}/{name}"] = {
                "any_of": _metrics_json(summary["any_of"]),
                "chunk_exact": _metrics_json(summary["chunk_exact"]),
                "by_category": {
                    k: _metrics_json(v) for k, v in summary["by_category"].items()
                },
            }
    (args.artifact_dir / "report.json").write_text(
        json.dumps(report, indent=2) + "\n", "utf-8"
    )
    print(f"Report written: {args.artifact_dir / 'report.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
