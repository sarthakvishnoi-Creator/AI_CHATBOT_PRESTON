# AGENTS.md

## Project Role
AI coding agents (Claude Code, Antigravity, etc.) are implementers, not
autonomous architects.

Follow approved architecture, technology decisions, repository structure,
and the current project phase. Do not move to another phase or make major
architectural changes without human approval.

#### Editing
- Use structured file editing for source changes.
- Do not use sed/awk or shell text-rewrite commands to modify source files.
- Use shell commands for inspection, installation, testing, linting, type checking, and Git.

## Verification
After meaningful code changes, run:
- uv run ruff format --check .
- uv run ruff check .
- uv run pyright
- uv run pytest
Report the actual results.

## Current Phase

Phase 4 — Backend Core / FastAPI — complete.
Phase 5 — Database & Data Layer — complete.

Current phase: Phase 6 — Knowledge Base.
Phase 6.2 — Canonical Foundation — complete.
Phase 6.3A — Incremental ingestion / synchronization foundation — complete.
Phase 6.3B-1 — Blog HTML cleaning and block construction — complete.
Phase 6.3B-2 step 1 — Blog MySQL table and column allow-list — complete.
Phase 6.3B-2 step 2 — Blog pre-clean gate — complete.
Phase 6.3B-2 steps 3–9 — the Blog adapter (`backend/preston/sources/blog.py`)
— complete. Verified against the real controlled MySQL corpus (read-only):
368 Blogs → 368 SourceRecords, 0 extraction failures.

`NORMALIZER_VERSION` reached 2 in the same change that wired
`parse_fragment` into the hash-bearing path, per B1 §4. It is now 3 — see
the FAQ atomic-retrieval entry below.

Phase 6.4-A — pre-ingestion readiness audit — complete (read-only; no
ingestion performed).
Phase 6.4-B0 — ingestion composition root
(`backend/preston/ingest_blogs.py`) — complete. It wires Settings → both
engines → `BlogAdapter` → `synchronize`, and is reached only by
`python -m preston.ingest_blogs`; nothing imports it, so neither Preston
nor FastAPI startup can begin a run.

Phase 6.4-B1 — the first real ingestion run — complete. Four successful
`sync` runs on 2026-09-15 (the first NEW = 368, 0 failures) established
368 Blog documents in PostgreSQL, at the time stored under
`normalizer_version` 2 and `chunker_version` 1.

Phase 6.4-C — Blog FAQ atomic retrieval — complete. `build_chunks`
(`backend/preston/ingestion.py`) builds chunks from canonical blocks: one
`FaqPair` is exactly one `document_chunk`, never windowed and never
merged with neighbouring prose, and a pair longer than `CHUNK_SIZE` still
stays whole. `CHUNKER_VERSION` 1 → 2, and `build_faq_pair` now requires an
answer that renders to text, which took `NORMALIZER_VERSION` 2 → 3.

Phase 6.4-D — controlled reprocessing — complete. Run
`1e33a5e9-5a26-4c4f-b5dc-0e60e6b5b326` (2026-09-16) reported
REPROCESSED = 368 with NEW, UNCHANGED, CHANGED, RESTORED and
`missing_candidates` all 0, no extraction, validation or persistence
failures, a complete inventory of 368, reconciled, exit code 0. All 368
content hashes came back identical to their pre-run values: the corpus
did not move, only our rules did, which is why REPROCESSED and not
CHANGED is correct. It rebuilt `document_chunks` 5,401 → 5,582 and
cleared `gone_count` 62 → 0 through ordinary application logic — no
manual reset.

Phase 6.4-E — test database isolation — complete. The suite resolves
`PRESTON_TEST_DATABASE_URL` and nothing else, and refuses to run if it
names the same database as `PRESTON_DATABASE_URL`, so tests can no longer
reach the ingestion corpus.

Run `ef1c23ab-c89a-489e-83c3-c11bf3b43a60` (2026-09-17) ingested the
promoted 402-blog controlled snapshot: NEW = 34, UNCHANGED = 367,
CHANGED = 1, all failure counters 0.

Phase 6.5 — service-page ingestion — complete for the four audited
families plus the frontend FAQ set. One generic adapter
(`backend/preston/sources/service_page.py`) serves every MySQL
service-page family through a declarative `FamilySpec`; there is no
family-specific adapter class, registry or factory. Each family was
audited read-only, implemented, reviewed and then ingested in exactly one
controlled run of its own scope:

| Family | `source_scope` | Run (2026-09-22) | Documents |
| --- | --- | --- | --- |
| Management Training | `management_training` | `c9b9b243` | 19 |
| Service FAQ (frontend `faq-data.ts`) | `service_faq` | `e61fb7d9` | 8 |
| GRC | `grc` | `407c8d84` | 38 |
| Audit & Assessment | `audit_assessment` | `3ceb870c` | 13 |
| Security Testing | `security_testing` | `3df582a8` | 14 |

Each run reported NEW = its full inventory with every other outcome 0, no
extraction, validation or persistence failures, a complete and reconciled
inventory, and touched no other scope. Professional Training remains
**unaudited and unimplemented** — deliberately absent from
`service_page.py` until it is audited.

**Current verified state** (final KB integrity audit, 2026-09-22,
read-only — verdict PASS). PostgreSQL holds **494 documents and 6,850
`document_chunks`**, all documents `active`, 0 archived, `gone_count` 0
and `consecutive_failure_count` 0 throughout, at `normalizer_version` 3 /
`hash_version` 1, chunked at `chunker_version` 2 (`extractor_version` 2
for Blog, 1 for every service-page family and the FAQ set). 11
`ingestion_runs`, all `succeeded`, none running, each touching exactly one
scope.

| `source_scope` | Documents | Chunks |
| --- | --- | --- |
| `blog` | 402 | 6,514 |
| `management_training` | 19 | 58 |
| `service_faq` | 8 | 28 |
| `grc` | 38 | 145 |
| `audit_assessment` | 13 | 56 |
| `security_testing` | 14 | 49 |
| **Total** | **494** | **6,850** |

The audit re-extracted all 494 documents from the controlled source and
compared them against stored state: every content hash was identical, with
0 extraction failures — the KB has not drifted from its source. Duplicate
canonical URIs, orphan chunks, empty chunks, `chunk_index` gaps and
placeholder blocks are all 0, and `alembic check` reports no schema drift.

Embeddings remain **not implemented**: no vector column exists and no
embedding model is approved. Next phase: **Phase 7 — Embedding & Vector
Retrieval**, gated on OPEN-3.

Version changes are no longer free: 494 documents are stored, so any bump
to `NORMALIZER_VERSION` (3) or `HASH_VERSION` (1) makes the whole corpus
REPROCESSED, and a bump to `EXTRACTOR_VERSION` reprocesses every scope
carrying that adapter's version. `CHUNKER_VERSION` (2) is the exception,
and not a safe one: it is stored per run, never per document, so a
chunker-only bump reprocesses nothing and leaves stored chunks stale. See
`docs/PHASE_6_KB_ARCHITECTURE.md` §7.1.

## Simplicity
Prefer the smallest production-quality solution.

- Reuse existing code before creating abstractions.
- Do not create files, classes, interfaces, wrappers, or dependencies
  without a current requirement.
- Avoid speculative architecture and future-proofing.
- Prefer simple modules over unnecessary layers.
- Do not duplicate framework functionality.
- Keep the codebase small, readable, and maintainable.
- Add complexity only when a concrete requirement justifies it.

## Development Rules
- Inspect relevant existing code before modifying it.
- Make the smallest reasonable change.
- Keep code simple, readable, and maintainable.
- Avoid unnecessary abstractions and dependencies.
- Reuse existing utilities when appropriate.
- Do not create unnecessary files.
- Do not modify unrelated files.

## Dependencies
- Check whether the standard library or an existing dependency is sufficient.
- Add dependencies only when required.
- Prefer stable, maintained libraries.
- Avoid duplicate tools or libraries serving the same purpose.
- Do not add dependencies speculatively.

## AI / RAG
Do not introduce new LLMs, embedding models, vector stores, or agent
frameworks without approval.

Keep provider-specific code behind replaceable interfaces and avoid direct
provider SDK calls from application/business logic.

## Database
PostgreSQL + pgvector is the approved database foundation.

Do not create or modify application schemas without an approved task.
Do not bypass migrations once they are established.
Do not introduce another database without approval.

## Testing
For meaningful code changes:
- Run relevant tests.
- Run relevant lint/type checks when applicable.
- Fix failures caused by the change.
- Report what was changed and verified.
- Never claim a test passed unless it was actually run.
- Do not create unnecessary tests or tooling just to make a check pass.

## Security
- Never commit `.env` or secrets.
- Never hardcode passwords, API keys, or tokens.
- Do not print secrets in logs or responses.
- Do not disable security controls just to make something work.

## Git
- Do not commit unless explicitly requested.
- Do not force push, reset, or discard user work without approval.
- `main` is the stable branch.
- Use `feature/<short-description>`, `fix/<short-description>`, or
  `chore/<short-description>` for development branches.

## Workflow
1. Understand the task.
2. Inspect relevant files.
3. Make the required changes.
4. Verify the changes.
5. Report what changed and what was verified.
6. Stop when the requested task is complete.

Ask for approval before:
- changing approved architecture or technology
- adding a new external service
- changing production infrastructure
- performing destructive database operations
- overwriting existing user work