# Preston AI Chatbot

A production-oriented enterprise AI chatbot platform.

## Status

Phase 6 — Knowledge Base. Complete: 6.2 Canonical Foundation, 6.3A
synchronization foundation, 6.3B-1 HTML cleaning and block construction,
the Blog allow-list, pre-clean gate and Blog adapter (6.3B-2 steps 1–9),
and Phase 6.5 service-page ingestion for four families plus the frontend
FAQ set.

PostgreSQL holds **494 documents** and **6,850 `document_chunks`** across
six source scopes, all documents active, at `normalizer_version` 3 and
`hash_version` 1, last chunked at `chunker_version` 2:

| Source scope | Documents | Chunks |
| ------------ | --------- | ------ |
| `blog` | 402 | 6,514 |
| `management_training` | 19 | 58 |
| `service_faq` | 8 | 28 |
| `grc` | 38 | 145 |
| `audit_assessment` | 13 | 56 |
| `security_testing` | 14 | 49 |

The five MySQL service-page families share **one** generic adapter
(`backend/preston/sources/service_page.py`), specialized per family by a
declarative `FamilySpec` rather than a subclass — Professional Training is
the one family not yet audited or ingested.

FAQ pairs are atomic retrieval units in stored data: each of the **516
complete FAQ pairs is exactly one `document_chunk`** — question and answer
together, never split by the character window and never merged with
surrounding prose.

A read-only final KB integrity audit on 2026-09-22 returned **PASS**:
every scope reconciles 1:1 with the controlled MySQL source, re-extraction
reproduced all 494 stored content hashes byte-for-byte, and no duplicate
URI, orphan chunk, empty chunk, placeholder block or navigation
contamination exists. See `FLOW.md` §13.

Next: **Phase 7 — Embedding & Vector Retrieval**. Chunking beyond FAQ
atomicity, embeddings and retrieval remain designed but **not
implemented** — there is no vector column and no approved embedding model.

## Running an ingestion

There are two composition roots for the batch job. Each builds the
PostgreSQL engine and the read-only controlled-MySQL engine from
`Settings`, constructs the adapter, and hands both to the existing
`preston.sync.synchronize` engine, which owns the run row, the advisory
lock and the per-document transactions. They are invoked deliberately and
never run on their own — nothing imports them, so starting the API cannot
begin an ingestion:

```
uv run python -m preston.ingest_blogs

uv run python -m preston.ingest_service_pages                  # default scopes only
uv run python -m preston.ingest_service_pages grc              # one named scope
```

Known service-page scopes are `management_training`, `grc`,
`audit_assessment`, `security_testing` and `service_faq`. A bare
invocation runs only `management_training` and `service_faq`; every other
family must be named explicitly, so no one can ingest a family by running
the module out of habit. Each scope runs independently, takes its own
advisory lock, gets its own run row, and reconciles only its own scope —
one family's failure never withholds another's.

Exit codes: `0` the run succeeded, `1` it failed (the run row records
why) or an unknown scope was named, `2` another run holds the advisory
lock and this process did no work. Both `PRESTON_DATABASE_URL` and
`PRESTON_SOURCE_MYSQL_URL` must be set or the command refuses before
building anything (the `service_faq` scope needs no MySQL).

Phase 4 delivered the FastAPI backend core: the application factory,
configuration, request lifecycle (startup/shutdown logging), API
versioning under `/api/v1`, problem-details error handling, request-id
correlation, structured logging, and liveness/readiness endpoints.

Phase 5 delivered the database access layer: the session/engine layer,
Alembic migrations, pgvector, and the conversation/document schema.
PostgreSQL + pgvector are live, not just development infrastructure.

Phase 6.2 delivered the canonical document foundation: deterministic
text/URL normalization, the canonical document model and content hash,
and ingestion change detection — see `docs/archive/phase-history/PHASE_6_2_COMPLETION.md` and
`FLOW.md`. Source extraction and chunking now exist; embeddings,
retrieval and the chat/RAG endpoint do not. An earlier plan to
crawl the rendered website (Playwright) is superseded: sources are
extracted directly from MySQL and the REST API, with the website used
only as a visibility verifier — see `FLOW.md` §2 and §11.

## Environment

- WSL2 + Ubuntu
- Python 3.12
- uv
- Node.js 22
- Docker Desktop
- PostgreSQL 18
- pgvector

The PostgreSQL + pgvector development database runs locally via
`docker-compose.yml` and is bound to `127.0.0.1:5432`. Local credentials are
supplied through `.env`, which is git-ignored; `.env.example` documents the
required variable names.

## Repository

| Path        | Purpose                                            |
| ----------- | -------------------------------------------------- |
| `backend/`  | FastAPI backend application.                        |
| `frontend/` | Future web frontend.                                |
| `tests/`    | Cross-component and integration testing.            |
| `docs/`     | Project and architecture documentation.             |
| `scripts/`  | Development and maintenance utilities.              |

`docs/` now holds the architecture, contract and decision-record documents
referenced above. `scripts/` holds read-only development utilities (the
service-page API cross-check and the frontend FAQ extractor); they are
never imported by the application. `frontend/` is still intentionally
empty at this stage.
