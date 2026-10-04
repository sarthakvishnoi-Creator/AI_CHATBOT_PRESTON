# Preston AI Chatbot

A production-oriented enterprise AI chatbot platform for INTERCERT.

## Status

Phases 4–8 are complete: the backend, the database, the knowledge base, the
embeddings and the retrieval layer. The next phase is **Phase 9 — a grounded
chatbot** on top of that retrieval layer. There is no chat endpoint and no
language-model answering yet.

| Phase | What it delivered |
| --- | --- |
| 4 | FastAPI backend core: application factory, configuration, API versioning under `/api/v1`, problem-details errors, request-id correlation, structured logging, liveness and readiness endpoints |
| 5 | Database layer: async engine and sessions, Alembic migrations, pgvector, the conversation and document schema |
| 6 | Knowledge-base ingestion: canonical document model, deterministic normalization and hashing, incremental synchronization, and source adapters for the blog, the five service-page families, the fixed-route pages, the frontend FAQ set and the diagram descriptions |
| 7 | Embeddings: schema, the OpenAI embedding provider, evaluation tooling, and the production backfill |
| 8 | Retrieval: exact vector search, two structured lookups, and a live validation against a golden question set |

### The knowledge base

PostgreSQL holds **541 documents** and **6,973 chunks** across 13 source
scopes. **All 6,973 chunks are embedded** (`text-embedding-3-large`, 3,072
dimensions, stored as `halfvec(3072)`).

| Source scope | Documents | Chunks |
| --- | --- | --- |
| `blog` | 402 | 6,514 |
| `grc` | 38 | 145 |
| `image_descriptions` | 26 | 57 |
| `management_training` | 19 | 58 |
| `security_testing` | 14 | 49 |
| `audit_assessment` | 13 | 56 |
| `professional_training` | 13 | 14 |
| `service_faq` | 8 | 28 |
| `resource_process` | 4 | 19 |
| `corporate` | 1 | 16 |
| `standalone_faq` | 1 | 10 |
| `privacy_policy` | 1 | 4 |
| `office_locations` | 1 | 3 |

Every FAQ question-and-answer pair is exactly one chunk, never split and
never merged with surrounding text. Sources are extracted directly from the
controlled MySQL database and the REST API; the rendered website is used only
as a visibility check.

### Retrieval

`backend/preston/retrieval.py` provides three read-only functions that return
structured `Evidence` (text, provenance and a public citation URL):

- `search_knowledge()` — exact vector search over an explicit allow-list of
  public scopes; the best two chunks per document, up to eight documents.
- `get_office_locations()` — a direct read of the office-locations document.
- `get_company_profile()` — a direct read of the first two chunks of the
  `/about` page, for facts about INTERCERT itself.

None of them calls a language model. A live validation against the 51-question
golden set (`scripts/validate_retrieval_live.py`) found the expected document in
the top eight for 37 of 40 answerable vector questions, with an end-to-end p95
of about 0.8 s. Similarity score alone is not used to decide whether a question
is answerable.

For the full picture see `FLOW.md` (how knowledge flows from source to
retrieval) and `AGENTS.md` (project rules, current state and decisions).

## Running things

Everything below is deliberate: nothing runs at import time, and starting the
API never begins an ingestion or an embedding run.

### Ingestion

```
uv run python -m preston.ingest_blogs

uv run python -m preston.ingest_service_pages                  # default scopes only
uv run python -m preston.ingest_service_pages grc              # one named scope
```

`ingest_service_pages` knows these scopes: `management_training`, `grc`,
`audit_assessment`, `security_testing`, `resource_process`, `privacy_policy`,
`corporate`, `professional_training`, `standalone_faq`, `office_locations`,
`service_faq` and `image_descriptions`. A bare invocation runs only
`management_training` and `service_faq`; every other scope must be named, so
nobody ingests a family by running the module out of habit. Each scope takes its
own advisory lock, gets its own run row, and reconciles only its own documents.

Exit codes: `0` the run succeeded, `1` it failed (the run row records why) or an
unknown scope was named, `2` another run holds the advisory lock and this
process did no work. `PRESTON_DATABASE_URL` and `PRESTON_SOURCE_MYSQL_URL` must
be set (the `service_faq` and `image_descriptions` scopes need no MySQL).

### Embedding backfill

Ingestion never calls the embedding provider; a separate job fills in the
vectors. **It is a dry run unless `--execute` is given.**

```
uv run python -m preston.embed_chunks                          # dry run: counts and estimated cost
uv run python -m preston.embed_chunks --execute --scope grc    # embed one scope
uv run python -m preston.embed_chunks --execute                # embed everything pending
```

Options: `--scope` (repeatable), `--limit`, `--max-cost-usd` (default 1.00). It
embeds only chunks that have no vector or one from a different model, so it
resumes after an interruption and does nothing once the corpus is complete.
`PRESTON_OPENAI_API_KEY` is required only with `--execute`. A document that is
re-chunked has to be backfilled again.

### Live retrieval validation

```
uv run python scripts/validate_retrieval_live.py --run-api
```

Read-only against the database; the only external call is embedding the golden
questions. Reports go to the git-ignored `evaluation_artifacts/`.

### The API

```
cp .env.example .env              # then set real values
docker compose up -d              # PostgreSQL + pgvector on 127.0.0.1:5432
uv sync
uv run alembic upgrade head
uv run uvicorn preston.main:app --reload
```

`/api/v1/health` reports liveness; `/api/v1/ready` also checks the database.
Interactive API docs are at `/docs`.

### Tests and checks

```
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest
```

Database-backed tests use a **separate** test database,
`PRESTON_TEST_DATABASE_URL`, and never the ingestion database; the fixtures
refuse a URL that names the same database as `PRESTON_DATABASE_URL`. If it is
unset those tests skip. Create and migrate it once, as `.env.example`
describes. A few tests also read the controlled MySQL source and skip cleanly
when it is unreachable. No test calls OpenAI.

## Configuration

Settings are read from environment variables prefixed `PRESTON_`, or from a
git-ignored `.env`. `.env.example` documents the variable names.

| Variable | Used for |
| --- | --- |
| `PRESTON_DATABASE_URL` | The application and ingestion database |
| `PRESTON_TEST_DATABASE_URL` | The dedicated test database |
| `PRESTON_SOURCE_MYSQL_URL` | The controlled MySQL source (read-only) |
| `PRESTON_OPENAI_API_KEY` | Embeddings (only for `--execute` and the live validation) |

Never commit `.env` or any key.

## Environment

- WSL2 + Ubuntu
- Python 3.12
- uv
- Node.js 22
- Docker Desktop
- PostgreSQL 18
- pgvector

The PostgreSQL + pgvector development database runs locally via
`docker-compose.yml` and is bound to `127.0.0.1:5432`.

## Repository

| Path | Purpose |
| --- | --- |
| `backend/` | FastAPI backend, ingestion, embedding and retrieval code |
| `alembic/` | Database migrations |
| `tests/` | Unit, database-backed and integration tests |
| `scripts/` | Development and evaluation utilities; never imported by the application |
| `docs/` | Architecture, contract and decision-record documents (kept local, not in git) |
| `frontend/` | Future web frontend — still intentionally empty |
| `FLOW.md` | The source-to-retrieval flow, and what is built and what is not |
| `AGENTS.md` | Project rules, the current phase and approved decisions |
