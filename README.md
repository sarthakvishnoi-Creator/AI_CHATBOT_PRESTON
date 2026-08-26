# Preston AI Chatbot

A production-oriented enterprise AI chatbot platform.

## Status

Phase 5 — Database & Data Layer (starting).

Phase 4 delivered the FastAPI backend core: the application factory,
configuration, request lifecycle (startup/shutdown logging), API
versioning under `/api/v1`, problem-details error handling, request-id
correlation, structured logging, and liveness/readiness endpoints. No
database access layer exists yet; PostgreSQL + pgvector currently run
only as development infrastructure.

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

`frontend/`, `docs/`, and `scripts/` are intentionally empty at this stage.
