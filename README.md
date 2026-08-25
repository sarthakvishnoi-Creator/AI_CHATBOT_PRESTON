# Preston AI Chatbot

A production-oriented enterprise AI chatbot platform.

## Status

Development foundation / Phase 3.

No application code has been implemented yet. This repository currently
contains only the development environment and the directory skeleton that
later phases will build into.

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
| `backend/`  | Future FastAPI/backend application.                 |
| `frontend/` | Future web frontend.                                |
| `tests/`    | Cross-component and integration testing.            |
| `docs/`     | Project and architecture documentation.             |
| `scripts/`  | Development and maintenance utilities.              |

These directories are intentionally empty at this stage.
