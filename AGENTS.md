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

Phase 6 — Knowledge Base ingestion — complete (details below). The current
phase is Phases 7–8 (embedding and retrieval), described after the Phase 6
status.
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

Phase 6.5 — service-page ingestion — complete for all five service-page
families plus the frontend FAQ set. The first four audited families and
the FAQ set are tabled below; Professional Training followed. One generic
adapter
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
inventory, and touched no other scope.

Since that table, three more sources have been implemented and ingested,
each in runs of its own scope:

- **Professional Training** (`professional_training`, 13 documents) — the
  fifth `FamilySpec` in `service_page.py`, ingested 2026-09-23. No written
  audit record for this family exists in `docs/`.
- **Fixed-route pages** (`backend/preston/sources/fixed_pages.py`,
  `FixedPageAdapter`) — `resource_process` (4), `privacy_policy` (1),
  `corporate` (1), `standalone_faq` (1), `office_locations` (1), ingested
  2026-09-23.
- **Image descriptions** (`backend/preston/sources/image_descriptions.py`,
  `ImageDescriptionAdapter`, `source_scope` `image_descriptions`, 26
  documents) — reviewed diagram descriptions produced offline by a vision
  model and read from the committed dataset
  `image_descriptions_data.json`; Preston itself calls no model. Ingested
  2026-09-27.

**Last full audit** (final KB integrity audit, 2026-09-22, read-only —
verdict PASS). At that time PostgreSQL held **494 documents and 6,850
`document_chunks`** across the six scopes in the table below, all
documents `active`, 0 archived, `gone_count` 0 and
`consecutive_failure_count` 0 throughout, at `normalizer_version` 3 /
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

**Current stored state** (read-only query, 2026-09-27; not a re-audit).
PostgreSQL holds **541 documents and 6,973 `document_chunks`** across 13
scopes, all `active`, `gone_count` 0 and `consecutive_failure_count` 0
throughout, at `normalizer_version` 3 / `hash_version` 1. 29
`ingestion_runs`, all `succeeded`. Migration head `54b5ec347e84` (Phase 7A).

| `source_scope` | Documents | Chunks | Stored `chunker_version` |
| --- | --- | --- | --- |
| `blog` | 402 | 6,514 | 3 |
| `grc` | 38 | 145 | 2 |
| `image_descriptions` | 26 | 57 | 3 |
| `management_training` | 19 | 58 | 2 |
| `security_testing` | 14 | 49 | 2 |
| `audit_assessment` | 13 | 56 | 2 |
| `professional_training` | 13 | 14 | 2 |
| `service_faq` | 8 | 28 | 2 |
| `resource_process` | 4 | 19 | 2 |
| `corporate` | 1 | 16 | 2 |
| `standalone_faq` | 1 | 10 | 2 |
| `privacy_policy` | 1 | 4 | 2 |
| `office_locations` | 1 | 3 | 2 |
| **Total** | **541** | **6,973** | |

Current phase: **Phases 7–8 — Embedding & Retrieval** (the foundation, the
evaluation and the retrieval layer are built; the production embedding backfill
and live validation are pending — see below). OPEN-3 is resolved by these
approved decisions:

| Decision | Approved |
| --- | --- |
| Provider | OpenAI |
| Model | `text-embedding-3-large`, 3,072 dimensions |
| PostgreSQL type | `halfvec(3072)` on `document_chunks.embedding` |
| Embedding identity (`embedding_model`) | `openai:text-embedding-3-large:3072` |
| Python dependency | `pgvector` |
| Initial search | Exact vector search |
| HNSW | Deferred; future baseline `m=16`, `ef_construction=64` |

Phase 7A — embedding schema foundation — complete (migration
`54b5ec347e84`). `document_chunks` gained `content_hash` (NOT NULL, B-tree
indexed; `hash_content` of the chunk's exact text, the identity an
embedding is keyed on — chunk ids do not survive a rebuild), `embedding
halfvec(3072)` and `embedding_model`, which are null together or set
together (`ck_document_chunks_embedding_model`).
`ingestion_runs.extractor_version` may be null only for `mode =
'backfill'` (`ck_ingestion_runs_extractor_version`). `_rebuild_chunks`
writes each chunk's `content_hash`. The migration filled and verified the
hash for all 6,973 existing chunks without changing any chunk or document.

**Embedding provider — complete.** `backend/preston/embedding.py` holds the
`Embedder` protocol and the OpenAI implementation. It validates every response
(count, order, dimension), disables the SDK's own retries, and reports the
requests and prompt tokens it was billed. Settings (`core/config.py`) default
to `text-embedding-3-large` / 3,072 dimensions and add an embedding request
timeout (default 10 s); the API key is a `SecretStr`.

**Embeddings have NOT been backfilled.** Production holds 541 documents,
6,973 chunks and **0 embeddings**: every `embedding` and `embedding_model` is
null. Ingestion never calls the provider; the backfill will be a separate job
and is not yet written or run. A rebuilt chunk is inserted without an
embedding, so changed documents are re-embedded by that backfill.

**Evaluation — complete (evaluation-only).** Golden Retrieval Set v2,
`tests/fixtures/golden_retrieval_set.json`: 51 cases (42 answerable, 9
unanswerable), identified by `canonical_uri` and chunk `content_hash` and
validated against the KB by `tests/test_golden_retrieval_set.py`. The offline
evaluators are `scripts/evaluate_embeddings.py` (model comparison, resumable),
`evaluate_chunk_context.py` and `evaluate_retrieval.py`; their vectors and
reports live under the gitignored `evaluation_artifacts/` and are
**evaluation-only**: they are never copied into `document_chunks`. Small and
Large embeddings performed closely on the golden set; the approved production
model remains `text-embedding-3-large`. The **chunk-context experiment kept the existing chunk text**: embedding
`{title} › {heading path}` + the chunk did not justify changing the production
representation, so the embedded text is the stored chunk content, unprefixed.

**Phase 8 retrieval — 8A–8D complete** (`backend/preston/retrieval.py`). The
design is intentionally simple.

- `search_knowledge(session, embedder, query, *, embedding_model, scopes=None,
  documents=8) -> list[Evidence]` validates the query (non-empty, at most 2,000
  characters), embeds it, runs **exact pgvector cosine search** over chunks that
  are in the explicit `PUBLIC_SCOPES` allow-list (13 scopes, listed one by one;
  never "all scopes"), in an active document, and embedded with the requested
  model; it groups candidates by document, keeps up to **2 chunks per
  document**, orders documents by their best score (ties broken by chunk hash)
  and returns `Evidence`. It raises `InvalidQueryError` for an unusable query
  and `RetrievalError` for an embedder or database failure, a malformed vector,
  or zero chunks embedded with the model (provider text is never passed on); no
  match is `[]`. It is read-only.
- `get_office_locations(session)` makes one structured read of the active
  `office_locations` document and returns its 3 stored chunks as `Evidence`. It
  uses no embeddings, so it works before the backfill, and does no per-office
  parsing or matching.
- `Evidence` carries `text`, `chunk_content_hash`, `canonical_uri`, `title`,
  `source_scope`, `content_type`, `chunk_index`, `retrieval_method`, `rank` and
  `citation_uri`, plus `score` and `embedding_model` for vector results and an
  optional `document_content_hash`. It never carries raw metadata or internal
  ids. An image description cites its parent service page only when its mapping
  is confirmed, and never exposes `preston-image://` as a citation.
- **Not part of retrieval:** lanes, source quotas, lane merging, full-text
  search, rank fusion (RRF), reranking, an HNSW index, any answerability
  decision, any LLM call, LangGraph, an API endpoint. `retrieval.py` still
  contains the 8A lane helpers (`LANES`, `RetrievalLimits`, `select`); only the
  offline 8B evaluator uses them, and production search does not.

**Next work:** the embedding backfill, then live validation of Golden Set v2
against `search_knowledge` and a measured search latency. Phase 8 is not
finished until that validation has run.

Version changes are no longer free: 541 documents are stored, so any bump
to `NORMALIZER_VERSION` (3) or `HASH_VERSION` (1) makes the whole corpus
REPROCESSED, and a bump to `EXTRACTOR_VERSION` reprocesses every scope
carrying that adapter's version.

`CHUNKER_VERSION` is now **3** (2 → 3 when image alt text listed in
`NON_KNOWLEDGE_ALT_TEXT` stopped reaching chunk text; blocks and hashes
unchanged). It is stored per document as well as per run: migration
`8fc561d8753e` added `documents.chunker_version`, and
`ingestion.py::_versions_match` compares it, so a chunker-only bump makes
each affected document REPROCESSED on its scope's next run. `blog` was
reprocessed under version 3 (run `eb74c09c`, 2026-09-23, REPROCESSED =
402) and `image_descriptions` was ingested under it; the other 11 scopes
still store version 2 and will report REPROCESSED on their next run.

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