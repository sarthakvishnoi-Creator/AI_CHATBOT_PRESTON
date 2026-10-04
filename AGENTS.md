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

Phase 6 — Knowledge Base ingestion — complete (details below).
Phases 7–8 (embedding and retrieval) — complete, described after the Phase 6
status. **Phase 9** (grounded answering on top of the retrieval layer) is in
progress: 9A and 9B complete, 9B-iv (documentation) current, **9C next**;
details below.
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
The chunk and document counts below are unchanged by Phase 7D, which only
populated embedding fields (see the Phase 7D status further down).

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

Phases 7–8 — **Embedding & Retrieval — complete** (7A schema, 7B provider,
evaluation, 7D production backfill, 8A–8D retrieval, 8H live validation; each
is recorded below). OPEN-3 is resolved by these approved decisions:

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

**Phase 7D — embedding backfill — complete** (`backend/preston/embed_chunks.py`,
`python -m preston.embed_chunks`). It populates only `document_chunks.embedding`
and `embedding_model`; it does not change any chunk text, chunk hash or
document. Ingestion still never calls the provider, and a rebuilt chunk is
inserted without an embedding, so a document that is re-chunked (for example a
REPROCESSED scope) must be backfilled again.

- **Behaviour.** A **dry run is the default**; writing needs `--execute`.
  Options: `--scope`, `--limit`, `--max-cost-usd` (default 1.00). It selects
  chunks of active documents in the explicit `PUBLIC_SCOPES` whose embedding is
  NULL or whose `embedding_model` is not `openai:text-embedding-3-large:3072`, so
  a rerun resumes naturally. It embeds each distinct `content_hash` once, in
  batches of at most 128, **outside any database transaction**, validates count,
  dimension and finite values, then writes each batch in one short transaction
  (`UPDATE … WHERE content_hash`, restricted to still-pending rows of active
  public documents). Requests use a 60 s timeout; a transient failure is retried
  up to three more times after 2, 4 and 8 s; a non-retryable error stops at
  once, as do three consecutive failed batches. It uses the ingestion advisory
  lock and records an `ingestion_runs` row with `mode = 'backfill'`,
  `extractor_version` NULL and the embedding identity. It never logs chunk text
  or provider messages. `synchronize(mode="backfill")` is rejected. No new table
  or migration.
- **Production result (verified read-only).** Controlled `image_descriptions`
  run (2026-10-01): 57 chunks, 1 request. Full backfill (2026-10-02): 6,916
  chunks, 55 requests, 1,067,502 prompt tokens, 0 failed batches, about 133 s
  (about $0.14 at the provider-reported tokens). The full-corpus rerun was
  **idempotent: 0 chunks selected, 0 API calls, 0 rows updated.**
- **Verified state.** 541 documents and 6,973 chunks, unchanged;
  **6,973 embeddings and 0 pending**, all `openai:text-embedding-3-large:3072`
  in `halfvec(3072)`, all 3,072-dimensional with finite positive norms. Every
  active public-scope chunk is embedded and none exists outside the approved
  public scopes. All 6,973 chunk hashes still match their text, and the
  document, chunk-hash and chunk-text fingerprints are identical to those taken
  before the backfill.
- **No HNSW index was added.** Exact vector search remains the production
  method.

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

**Phase 8 retrieval — 8A–8D complete, 8H live validation complete**
(`backend/preston/retrieval.py`). The design is intentionally simple.

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
  uses no embeddings (so it works whether or not a backfill has run) and does
  no per-office parsing or matching.
- `get_company_profile(session)` is a second structured read of the same kind,
  added after the company-fact diagnostic (below). It reads the active document
  whose canonical URI is exactly `https://www.intercert.com/about` and returns
  its **first two chunks** (`chunk_index` 0 and 1, in order) as `Evidence` with
  `retrieval_method = "structured"` and the public About URL as its citation. It
  uses no embeddings, no query and no semantic search, and calls neither the
  provider nor `search_knowledge`. A missing or inactive About document gives
  `[]`; a database failure raises `RetrievalError`. It shares one private
  structured read (`_read_structured`) with `get_office_locations`, which is
  otherwise unchanged. It is a targeted structured read over the existing
  knowledge base, not a retrieval redesign: `search_knowledge()`, dense
  retrieval, ingestion, chunks, embeddings and the schema are unchanged. It is
  available for the future chatbot layer (Phase 9); it does no routing and
  nothing calls it yet. The two chunks it returns carry the founding year, years
  operating, the company description, scale (clients, countries) and
  accreditations. **Limitation:** it selects chunks 0–1 *by position*; it does
  not locate company facts dynamically. A source-backed content guard
  (`test_real_about_page_leading_chunks_carry_the_company_profile`) builds the
  real About page from the controlled MySQL source and fails if a chunk or layout
  change moves those facts out of chunks 0 and 1. The guard depends on access to
  that MySQL source and **skips cleanly when it is unavailable**, so it is not an
  unconditional check.
  The dense retrieval architecture remains unchanged. A small structured
  company-profile read was added for authoritative company facts, following the
  existing `get_office_locations()` pattern. The current implementation reads the
  first two `/about` chunks; a source-backed content guard protects against
  future chunk/layout drift.
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

**Phase 8H — live retrieval validation — complete** (2026-10-02;
`scripts/validate_retrieval_live.py`, run with `--run-api`; reports in the
gitignored `evaluation_artifacts/retrieval-live/`, with no chunk text). It runs
the 51 Golden Set v2 queries (42 answerable, 9 unanswerable) through the real
`search_knowledge` with live `text-embedding-3-large` query embeddings, and
compares the result with a float32 replica of the same strategy computed from
the saved evaluation vectors. It is read-only and changed no retrieval, schema,
ingestion or production data.

- **Quality.** 37 of the 40 answerable vector cases hit the expected document in
  the top 8, identical to the float32 baseline, with no regressions; 5 of 5
  image cases passed (the 2 `structured` cases are checked through
  `get_office_locations` and are not in the vector metrics).
- **Fidelity.** The halfvec top-1 chunk matched float32 in 49 of 49 cases. Two
  cases differed only at the 8th-document cutoff, where two documents scored
  within about 0.001 of each other; they were reviewed as harmless rounding
  effects.
- **Latency.** End-to-end p50 about 0.56 s, p95 about 0.78 s, max about 1.81 s
  (target ≤ about 1 s p95). The query-embedding API call dominates (p50 about
  0.52 s); database search plus grouping was about 45 ms p50 and 57 ms p95, and
  `EXPLAIN (ANALYZE, BUFFERS)` execution was about 42 ms p50. **HNSW is not
  justified at the current corpus size and performance.**
- **Safety.** Public scopes only, no internal ids, no `preston-image://`
  citation. Query embedding cost was provider-reported: 51 requests, 610 prompt
  tokens.
- **Three misses reviewed** (`paraphrase-09`, `cross-03`, `exact-06`, the same
  three the float32 baseline misses): short-query near-ties and blog
  duplication explain one; a buyer-phrased paraphrase against a marketing-style
  page is uncertain; `cross-03` ("How long has INTERCERT been operating…") is a
  genuine limitation, recorded below.

**Known retrieval limitations (recorded, not a reason to redesign retrieval).**

- **Similarity score is not an answerability threshold.** Unanswerable queries
  scored as high as 0.708 while the weakest correct top hit scored 0.482, so
  scores overlap. Answerability must not be decided from score alone.
- **Canonical company facts under dense-only retrieval.** Facts that live in
  very few chunks (founding year, years operating, headquarters, the company
  description) can be buried by repeated blog boilerplate: "Why Choose
  INTERCERT" appears in 116 chunks across 99 blog documents. Facts repeated
  across many blog chunks (organizations certified, countries) retrieve well.
  This is a known weakness for canonical company facts, not a general retrieval
  failure. The facts on the About page are now also reachable through the
  structured `get_company_profile()`; dense search itself is unchanged, so the
  weakness still applies to any other single-source fact.
- **No source priority.** Dense search cannot prefer a canonical service page
  over blog posts on the same topic (blogs are about 93% of chunks), and on
  short identifier queries documents near the 8th rank can be within about
  0.005 of each other.
- Only 50 chunks are scored before grouping and at most 2 chunks per document
  are kept.

**Company-fact diagnostic** (read-only, after 8H, 2026-10-02). Eight company-fact
queries were run through the unchanged production path (8 requests, 55 prompt
tokens): organizations certified, headquartered, when founded, how long
operating, countries, "What is INTERCERT's headquarters?", "Who is INTERCERT?",
"What does INTERCERT do?". The expected evidence was taken from stored text
only. **2 of 8 retrieved supporting evidence** (organizations certified and
countries, both repeated across many blog chunks); **6 of 8 did not surface the
canonical single-source fact** (headquarters, founding, years operating, who and
what), where repeated "Why Choose INTERCERT" boilerplate was the main competing
signal. The expected chunk was always close (true rank about 20–68 of 6,973) but
outside the top 8. The eight queries cover only about four distinct facts, so
they are not eight independent observations. The diagnostic itself changed no
retrieval; it motivated the targeted `get_company_profile()` read above. A
deterministic check (no API calls, read-only against the production knowledge
base) confirmed that the authoritative evidence for all eight queries is now
available through the two structured tools: `get_company_profile()` for the six
company and scale questions and `get_office_locations()` for the two headquarters
questions.

**Phase 9A — agent foundation — complete (2026-10-03, not yet wired to any
API).** `backend/preston/agent.py`: the evidence boundary (labelled `<source>`
blocks, application-owned citations), the three read-only tools behind
`run_tool` (unknown names refused, one session and a timeout per call, every
failure a fixed category), the grounding system prompt, and the two-node
LangGraph loop (`run_turn`: the first round must call a tool, at most 3 model
rounds and 4 tool calls, an answer without any tool is discarded).
`backend/preston/chat_model.py` builds the OpenAI model and refuses to start
with no model configured or with tracing on. Tested offline with a scripted
fake model, a network guard, and one run through the real `ChatOpenAI` class
against a local fake server; no OpenAI call was made. No migration, schema,
retrieval, ingestion or embedding change.

**Phase 9B — conversation foundation — complete (2026-10-04, uncommitted; not
yet wired to any API).** Migration `e0384a7162ec_conversation_foundation`
extends `conversations` and `messages` and adds `tool_calls`,
`message_evidence` and `support_requests`; `backend/preston/conversations.py`
holds the lifecycle. **PostgreSQL is the product conversation history;
LangGraph stays stateless (no checkpointer, no LangGraph memory).** Ownership is
server-enforced (only a SHA-256 digest of the visitor token is stored; one
uniform 404; ownership before expiry); the 24-hour inactivity rule (shared
`ensure_active()`) applies to new turns and support requests; turns are two short
transactions with no lock held while the model runs; history is the last 10
messages of complete turns; tool calls and evidence *references* are audited;
support requests store the A5 contact details (name, email, company) and nothing
else does. `admin_access_log` is deferred to 9E. Defaults: 24 h inactivity, 30
turns, 10 history messages (settings). **The migration is rehearsed on the test
database only; the ingestion database has not been migrated (approval 9B-3).**
Detail: `docs/PHASE_9B_CONVERSATION_DESIGN.md`.

**Phase 9 plan — 9A and 9B complete; 9B-iv current; 9C (public chatbot) next;
9D–9F not started.** Phase 9 adds grounded
LLM answering on top of the existing retrieval layer; it does not change
retrieval. **Approved (2026-10-03):** LangGraph for agent execution and
LangChain for model/tool integration (`langgraph==1.2.2`,
`langchain-core~=1.6.6`, `langchain-openai~=1.6.7`; runtime-verified against
`openai` 3.22.1; `langgraph` is pinned exactly because ≥ 1.2.12 would force
`websockets` below 17). OpenAI remains the provider; **the exact chat model is
not yet approved**, so Phase 9 code runs on a fake model until then. The agent
is one simple graph: no planner, multi-agent system, LangGraph checkpointer,
Redis, queue, event bus or other orchestration infrastructure is approved.
LangSmith tracing stays off. The design is
`docs/PHASE_9_CHATBOT_ARCHITECTURE.md` (local; `docs/` is gitignored).

- **Grounded answering.** The LLM answers Intercert-specific questions only from
  retrieved `Evidence`. If sufficient evidence is not retrieved it must not
  invent an answer or rely on unsupported Intercert-specific knowledge; it says
  the information could not be verified from the available knowledge.
- **Evidence logging** (storage implemented in 9B: `tool_calls`,
  `message_evidence`). For every chatbot answer, record which `Evidence` items
  reached the LLM, so a failure can be classified as a retrieval failure
  (required evidence not retrieved) or a generation/grounding failure (evidence
  retrieved, answer unsupported or wrong).
- **Office / headquarters routing.** Use the existing `get_office_locations()`
  for office, headquarters and location questions where appropriate. General
  retrieval is not redesigned around this. The structured `get_company_profile()`
  is likewise available for questions about INTERCERT itself; when and how either
  tool is called is a Phase 9 decision and is not implemented.
- **Standing company-fact regression set.** The eight queries above are kept as
  a standing Phase 9 regression set, to observe how the complete chatbot
  behaves before deciding anything about retrieval.
- **Phase 9.x trigger (for investigation only).** Open a Phase 9.x
  retrieval-improvement investigation if more than 2 of the 8 standing queries
  produce an incorrect or unsupported chatbot answer *because the required fact
  was not present in the retrieved Evidence*. This is a trigger, not a statement
  that Phase 9.x is required; until it fires, no lanes, FTS, RRF, reranking,
  HNSW or answerability scoring are added because of this diagnostic.

**Architecture decisions that stand:** one simple `search_knowledge()` engine
over the explicit `PUBLIC_SCOPES` allow-list; exact vector search; best 2 chunks
per document, up to 8 documents; no production lanes, no FTS or RRF, no HNSW
yet, no retrieval Protocol or registry; `get_office_locations()` is the
structured office tool and `get_company_profile()` the structured company-profile
tool; retrieval returns structured `Evidence`.

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