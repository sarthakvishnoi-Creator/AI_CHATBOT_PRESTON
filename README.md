# Preston AI Chatbot

A production-oriented enterprise AI chatbot platform.

## Status

Phase 6 — Knowledge Base. Phase 6.2 — Canonical Foundation — complete.
Next: Phase 6.3 — Source Extraction.

Phase 4 delivered the FastAPI backend core: the application factory,
configuration, request lifecycle (startup/shutdown logging), API
versioning under `/api/v1`, problem-details error handling, request-id
correlation, structured logging, and liveness/readiness endpoints.

Phase 5 delivered the database access layer: the session/engine layer,
Alembic migrations, pgvector, and the conversation/document schema.
PostgreSQL + pgvector are live, not just development infrastructure.

Phase 6.2 delivered the canonical document foundation: deterministic
text/URL normalization, the canonical document model and content hash,
and ingestion change detection — see `docs/PHASE_6_2_COMPLETION.md` and
`FLOW.md`. Source extraction (MySQL/API adapters), chunking, embeddings,
retrieval and the chat/RAG endpoint do not exist yet. An earlier plan to
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
referenced above. `frontend/` and `scripts/` are still intentionally empty
at this stage.
