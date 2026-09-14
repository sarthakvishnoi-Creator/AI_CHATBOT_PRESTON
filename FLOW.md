# FLOW.md — Preston AI Knowledge Base Flow

> FLOW.md describes how the KB operates. It is not the detailed architecture
> specification. If the architecture changes, this flow must be updated.

**Reads:** `Preston_Source_of_Truth_FINAL` (2026-09-04) ·
`docs/PHASE_6_KB_ARCHITECTURE.md` (2026-09-07; accepted, partially
implemented — 6.2, B1, B2, B3, B4, 6.3A, 6.3B-1 and the Blog adapter
(6.3B-2 steps 1–9) complete; the first real PostgreSQL ingestion run
not yet performed) ·
`docs/PRESTON_PHASE_6_OPEN_QUESTIONS_DECISIONS.pdf` — the Implementation
Decision Record, which freezes the decisions needed to enter Phase 6.3 ·
`docs/archive/phase-history/PHASE_6_2_COMPLETION.md` (Phase 6.2, implemented)

**Labels used below, carried from the source documents:**
**CONFIRMED** (evidenced by the source contract or existing code) ·
**RECOMMENDED** (architectural judgment) · **OPEN** (needs approval or evidence) ·
**RESOLVED** (frozen by the decision record)

**Where the flow stands:** stages ①–⑤ up to the canonical seam are **built**
(Phase 6.2 — schema, URL normalization, the content/hash normalizer split, the
version quartet; migration head `b1d7e4a26c58`). Cleaning and block
construction (6.3B-1) and the synchronization foundation (6.3A) are now built
too. Source **adapters**, chunking, embeddings and retrieval remain
**designed, not built**. §12 says which decisions are frozen and which still
block.

---

## 1. The whole flow in one picture

```
 ┌─── SOURCES ──────────────────────────────────────────────────────────────┐
 │ MySQL adapter          REST API adapter        Website probe             │
 │ blogs · blog FAQ       GRC · audit · STC       NOT an extractor          │
 │ blog meta · registry   training ×2 · FAQ       visibility verdict only   │
 │ (registry = metadata)  corporate · resource                              │
 └──────┬─────────────────────────┬──────────────────────────┬──────────────┘
        │ SourceRecord            │ SourceRecord             │ VisibilityVerdict
        └───────────┬─────────────┘                          │  (advisory only)
                    ▼                                        │
   ① DISCOVER    inventory of composed canonical URIs        │
                    ▼          (abort on source failure)     │
   ② EXTRACT     allow-listed columns / endpoints            │
                    ▼                                        │
   ③ VALIDATE    structural plausibility — BEFORE the hash   │
                    ▼                                        │
   ④ NORMALIZE   content-norm  ‖  hash-norm (two functions)  │
                    ▼                                        │
 ═══ ⑤ CANONICAL DOCUMENT ══════ THE SEAM ═══════════════════│════════════════
     nothing below this line knows MySQL, DRF, HTTP or HTML  │
                    ▼                                        │
   ⑥ DEDUPE      url · doc · chunk · conflict (flag, never delete)
                    ▼                                        │
   ⑦ CHANGE      NEW │ UNCHANGED │ CHANGED │ REPROCESSED     │
                    │      └─ no-op, stop                    │
                    ▼                                        │
   ⑧ LIFECYCLE   inventory reconcile · mass-archival gate    │
                    ▼                                        │
   ⑨ CHUNK       heading tree · token budget · chunk hashes  │
                    ▼                                        │
   ⑩ EMBED       reuse by chunk hash · embed only remainder  │
                    ▼                                        │
   ⑪ PERSIST     one transaction per document ───────────────┤
                    ▼                                        │
 ┌──────────────────────────────────────────────────────┐    │
 │ PostgreSQL + pgvector                                │◄───┘
 │ documents · document_chunks · ingestion_runs         │  metadata.visibility.*
 └───────────────────────────┬──────────────────────────┘
                             ▼
   RETRIEVAL   query → FTS ∪ vector k-NN → RRF → SQL filter → Evidence[]
                             ▼
                  [ RAG answering layer — out of scope ]

 ABORT (zero writes):  inventory build failure · mass-archival gate
 KEEP LAST-KNOWN-GOOD: fetch failure · validation rejection · embedding outage
 NO-OP:                hash UNCHANGED
```

---

## 2. Where knowledge comes from — source roles

| Source | Role | Supplies content? | Label |
| --- | --- | --- | --- |
| **MySQL** (`ICWebDatabase`) | System of record. Every content byte originates here. Primary extraction for flat, slug-keyed content: blogs (368), blog-embedded FAQ pairs (≤1,840), blog SEO metadata | **Yes** | CONFIRMED |
| **REST API** (DRF, ~90 public endpoints) | Primary extraction for nested, section-composed page types: GRC (38), audit (13), STC (14), management-system training (19), professional training (13), corporate/resource, locations, accreditations. Also the **only** source for standalone FAQ, because `/faqs/` applies `is_active=True` — the single real publication gate in the stack. Separately: validates MySQL extraction fidelity by sampled comparison | **Yes** | CONFIRMED |
| **Website probe** (`www.intercert.com`) | **Verifier, never an extractor.** Emits a `VisibilityVerdict` into `metadata.visibility.*` only. Judges by *content identity* (distinct `<title>` + byte size diverging from the per-category fallback signature), **never by HTTP status** — Angular returns 200 for every path. **Advisory metadata only: a failed or unusual probe never blocks valid source extraction and can never archive a document by itself** (OPEN-12 RESOLVED) | **No** | CONFIRMED |
| **Registry / sitemap** (`subone_search` 89, `servicenav` 67, `nav` 85) | **Discovery aid and SEO reconciliation only. Never corpus, never a filter.** Q1 disproved it as a publication gate: 17 of 18 unregistered records are publicly live, and production's own sitemap ships 3 dead URLs | **No** | CONFIRMED |

**Why the site is not the content source:** its SSR payload is the API response
re-served — it holds nothing the API lacks, and no layer between MySQL and
rendered HTML removes anything. **CONFIRMED**

> **OPEN-1 — RESOLVED** by the decision record. MySQL and REST API are the
> authoritative extraction sources; the website is a verifier only;
> registry/sitemap is discovery and SEO reconciliation only. The competing
> layering in *Preston KB Architecture v1.1* (published page as the only content
> source) does **not** hold. The adapter seam stays as designed, so a future
> reversal would still replace one adapter and change nothing downstream.
>
> Note for readers of the older documents: `docs/PHASE_6_KB_ARCHITECTURE.md` §18
> and `docs/archive/phase-history/PHASE_6_2_COMPLETION.md` both still record OPEN-1 as blocking. The
> decision record (`docs/PRESTON_PHASE_6_OPEN_QUESTIONS_DECISIONS.pdf`)
> supersedes them on this point.

### 2.1 Development snapshot vs. production source — read this before trusting a run

| | Today (development) | Future (production) |
| --- | --- | --- |
| **MySQL** | Local `website_db`, the **point-in-time dump `ICWebDatabase_20260829`** (2026-08-29). Frozen. Never re-reads. Whether production has since diverged is **UNKNOWN** | Read-only access to prod RDS `ICWebDatabase` or a replica — route and lag **OPEN-4** |
| **Change detection** | Detects only changes *within the snapshot* — i.e. effectively nothing. A "no changes" run proves the pipeline is idempotent, **not** that the site is unchanged | Detects real editorial change |
| **Deletion / archival** | Reconciliation runs against a frozen inventory. Do not read archival counts as production signal | Real deletions detected by full-inventory reconciliation |
| **Governance** | The dump on a developer workstation contains client-certificate and contact-enquiry records. Provenance, authorization and retention must be settled with security **before further use** — **OPEN-5** | Read-only `SELECT`; no PII tables in the allow-list |

The snapshot is sufficient for build-out — Phase 6.3 may proceed against it,
provided the allow-list is followed and **no live-sync claim is made**. It is
**not** sufficient for live synchronization, and no incremental-sync claim made
against it transfers to production until OPEN-4 and OPEN-5 are approved.

> **Do not design correctness around the dump.** It proves pipeline behaviour and
> idempotency, never current production state. (Decision record, OPEN-4.)

---

## 3. How knowledge enters the KB

| # | Stage | In → Out | Rules that matter |
| --- | --- | --- | --- |
| ① | **Discover** | source PK sets, API listings, registry rows → deduplicated set of composed, normalized `canonical_uri` | `canonical_uri` is **composed** from a versioned Route Map (e.g. `/blogs/{url}`, `/services/governance-risk-compliance/{url_string}`), not observed. Recomputed from scratch each run, never persisted as authority. Discovery may only **add to the extraction list** — it may never assert a document exists. Three production-verified dead routes are deny-listed here. **Granularity: one canonical document per independently answerable public page or entity** — never one giant document for a whole category (OPEN-14 RESOLVED) |
| ② | **Extract** | inventory entry → `SourceRecord` | **Explicit table-and-column allow-list. No `SELECT *`, no wildcard endpoint consumption.** Side-effect-free, so retries are always safe |
| ③ | **Validate** | `SourceRecord` → pass, or a typed rejection (`empty_body`, `below_floor`, `yield_collapse`, `no_headings`, `placeholder_record`) | **Sits before the hash on purpose.** Hashing a broken extraction and calling it "changed" is exactly how an empty record overwrites good content |
| ④ | **Normalize** | one input → **two outputs**, never one | *Content-norm* preserves block boundaries (for storage and chunking). *Hash-norm* collapses to a flat serialization (for `content_hash` only). Both: NFC, NBSP/zero-width removal, canonical quotes and dashes, **case preserved** ("ISO" ≠ "iso"). Image `src` excluded from the hash (cache-busting churn). Volatile fragments stripped — left in, the whole corpus re-embeds nightly. **Pure, deterministic, zero LLM** |
| ⑤ | **Canonical document** | → `CanonicalDocument` | **The seam.** Ordered typed blocks — `Heading`, `Paragraph`, `ListBlock`, `FaqPair`, `Table`, `ImageRef` — plus non-authoritative `metadata` and `provenance`. Nothing downstream references MySQL, DRF or HTTP. `blocks` are authoritative; `metadata` never is and is never hashed |

**A property worth naming:** because extraction is record-first, navigation,
footers, consent banners and CTAs *never enter the pipeline at all* — they are
template artifacts, not content fields. The boilerplate problem that dominates
HTML extraction is largely designed out. **RECOMMENDED**

**Two things stage ② and ⑤ must not assume in 6.3:**

- **Rich-text fields carry real markup.** Section bodies are authored in Django
  admin, so ② needs an approved HTML5-conformant fragment parser and must handle
  ordered-list starts, tables, hidden/AI-pasted DOM artifacts and inline images
  per the cleaning contract — not a regex strip. (OPEN-15, dependency approval.)
- **FAQ questions are not necessarily plain text.** Evidence shows FAQ question
  fields can contain HTML/block-level markup. `FaqPair.question` is implemented
  today as `str`. 6.3 must inspect real records and define a deterministic
  normalization rather than silently discarding structure or forcing content into
  the wrong type. **OPEN-20 — DEFERRED to 6.3.**

---

## 4. Deduplication — detect, link, escalate; never merge

| Cause | Action |
| --- | --- |
| Re-ingesting the same record | Unique `canonical_uri` + content-hash no-op. **Already implemented** in `ingest_document` |
| URL variants (slash, `www`, case, tracking params) | Collapsed by one tested canonical-URL normalization function, before a row exists. **Query parameters sort deterministically by key then value, with duplicate parameters preserved as separate entries** (U9 RESOLVED, matching what is implemented). Changing this later is a `normalizer_version` bump that invalidates affected derived artefacts |
| Identical content at two URLs | Both retained, linked via `metadata.duplicate_of`; higher-authority copy retrieved, other archived-but-known. **Never silently dropped** |
| Repeated boilerplate blocks | Chunk-hash frequency within *and* across documents → `is_boilerplate = true`; excluded from retrieval, **retained in place** |
| **Conflicting content** | `metadata.conflicts_with` link + escalation report. **Detect, quarantine, escalate — never merge.** A merged statement is a sentence no source actually said |

Real conflicts on record: DORA has two live URLs against one DB row; ISO
20121:2012 has two live representations under different service categories. Both
are content-ownership decisions (**OPEN-6 / OPEN-7**), not engineering ones.
**CONFIRMED**

The decision record fixes the interim engineering behaviour: **preserve both live
representations as separate documents**, linked by duplicate/conflict metadata,
until the content owner decides canonical ownership. Never silently merge or
delete one. Deduplication must never create a statement that neither source
actually contains.

Deleting a chunk is never a dedup action — a chunk is positional within its
document, and removing one breaks reassembly and citation offsets.

---

## 5. How synchronization avoids a full rebuild

**The content hash is the only oracle of change.** 288 of 298 models carry no
lifecycle field, content models carry no `updated_at`, and `NewBlogs.status` is
unusable (77% empty, five encodings, read by no code path). There is no
timestamp-based alternative. **CONFIRMED**

Detection is layered: inventory set difference (finds new/removed) → source
metadata such as `django_admin_log.action_time` (**optimization only** — it may
trigger a cheaper re-extraction attempt, but it can **never** suppress extraction
or override `content_hash`; **OPEN-9 RESOLVED**) → content hash (the truth) →
chunk hash (decides what to re-embed).

| Outcome | Trigger | Writes | Chunks & vectors | Retrievable |
| --- | --- | --- | --- | --- |
| **NEW** | URI has no row | Insert, `status='active'`, full provenance | Chunk and embed all | Yes |
| **UNCHANGED** | Hash matches **and** versions match | `last_seen_at`, `last_run_id` only | Untouched | Yes |
| **CHANGED** | Hash differs, versions match | Blocks, hash, provenance | Rebuild; **reuse vectors for chunks whose hash is unchanged** | Yes |
| **REPROCESSED** | **Version tuple differs** — a deliberate bump | Blocks, hash, new versions | Rebuild; reuse where hashes still match | Yes |
| **ARCHIVED** | Absent from a *successfully built* inventory, or repeated gone-responses | `status='archived'`, `gone_count` | **Retained in full** | **No** — excluded by SQL predicate |
| **FAILED** | Extraction/validation failure after retries | `consecutive_failure_count` only; **`status` unchanged** | Untouched — last known-good kept | Yes |
| **RESTORED** | An archived URI reappears | `status='active'`, `gone_count=0` | Re-hashed; rebuilt only if changed | Yes |

**The version quartet** — `extractor_version`, `normalizer_version`,
`hash_version`, `chunker_version` — travels with every derived artefact. If the
stored tuple differs from the current one, the two hashes are **not
comparable**: the document is counted `REPROCESSED`, not `CHANGED`, and excluded
from the change-rate alarm. Without this, our own rule change is
indistinguishable from the entire corpus changing — or from a compromised
extractor. **RECOMMENDED**

**Run modes:** `sync` (both gates armed) · `reprocess` (mass-*change* gate
suppressed, because mass change is the expected outcome; **archival gate stays
armed**) · `backfill` (embeddings only; cannot archive).

**Why no queue, broker or checkpoint store:** every stage is idempotent, so
re-running the job *is* the retry mechanism. Completed documents hash as
`UNCHANGED` and cost nothing, so resumption is free.

---

## 6. Deletion → archival, and the gate that protects the corpus

No tombstones exist; source deletions are physical (66 logged blog deletions
against 435 admin-log ids vs 368 live rows). **Full-inventory reconciliation each
run is the only reliable detector**, and is cheap at this scale — 368 blogs plus
~98 pages. **CONFIRMED**

- **Archive, never delete.** Blocks, chunks and vectors are retained in full;
  restoration is a status flip plus a hash comparison. This reversibility is what
  makes unattended archival safe. **RECOMMENDED**
- **The mass-archival gate — now a number** (**OPEN-11 RESOLVED**, as a
  configurable recommendation):

| Rule | Value |
| --- | --- |
| Abort threshold | candidate archivals ÷ active corpus **> 10%** |
| Floor before the percentage means anything | **at least 10 candidates** |
| Complete MySQL inventory | **1 strike** may make absence archival-eligible |
| API-derived absence | **2 consecutive strikes** required |
| Website probe | **never archives** |

  When the gate trips, **the run aborts before any archival write** — it archives
  nothing. This is the single most important control in the system: without it,
  one bad source deploy turns the assistant into one that answers "I don't have
  information about that" for every question, while the run reports success.
  `reprocess` mode suppresses the mass-*change* alarm but **does not disable
  archival safety**. All values configurable.

---

## 7. Chunking → embeddings → storage

**Chunk** — deterministic and structure-aware, for a fixed
`(chunker_version, tokenizer_id)`:

- Primary split is the **heading tree**; each leaf section is a candidate chunk.
- Target ~400 tokens, hard max ~800, measured with the embedding model's own tokenizer.
- Undersized sections merge with siblings under the same parent, never across parents.
- **FAQ pairs are one chunk each, question and answer never separated.**
- Lists never split mid-item; tables never split (oversized → row groups repeating the header **where one exists** — no table in the current corpus marks a header, so there is nothing to repeat; contract §10.1.2, B3).
- Deterministic context prefix `{title} › {heading path}` is prepended to every chunk — and is therefore part of the embedded text and of the chunk hash.
- Identity is **both**: positional `(document_id, chunk_index)` for order and citation, semantic `content_hash` for reuse.
- A document producing zero chunks is a validation failure, not a valid empty document — roll back, keep the previous state.

**Embed** — reuse keyed on **chunk `content_hash` as a multiset**, never on chunk
index (index-keyed reuse silently pairs a vector with different text when content
shifts). Build a `hash → [vectors]` pool from prior committed chunks where
`embedding_model` matches, draw per new chunk, embed only the remainder. Two
properties follow for free: a model change yields zero reuse and a correct full
re-embed; and a title change correctly invalidates that document's chunks,
because the prefix really did change. **RECOMMENDED**

**Persist** — vectors live beside the text, in the same PostgreSQL database and
**the same transaction**. pgvector is already enabled (revision `0d2a9b650819`,
image `pgvector/pgvector:0.8.6-pg18-trixie`). **One transaction per document**, so
a crash leaves N complete documents and 0 partial ones, and stale text can never
sit beside a fresh vector. HNSW with cosine distance, iterative index scans on.
No separate vector store at ~10⁴ chunks. **CONFIRMED / RECOMMENDED**

> The `vector(N)` column and its HNSW index are **DEFERRED** — dimension is a
> hard PostgreSQL/pgvector schema commitment, and chunk boundaries depend on the
> model's tokenizer, so chunking is not final until it is resolved. **OPEN-3
> remains OPEN and blocks Phase 6.5.** The decision record is explicit that
> provider, model/version, dimension, tokenizer ID, data-handling / no-training
> terms and cost characteristics must all be frozen together after an approved
> evaluation — and that **no vector column is added before that approval**.

---

## 8. How knowledge reaches retrieval

```
query → normalize (no LLM in this path)
      → ① PostgreSQL FTS over document_chunks.fts (GIN)
        ② pgvector cosine k-NN over embedding (HNSW)          — in parallel
      → Reciprocal Rank Fusion over the two ranked lists
      → SQL predicate:  documents.status = 'active'
                        AND NOT document_chunks.is_boilerplate
                        AND answerability predicate (policy set; thresholds 6.6)
      → assemble: dedupe by document, order by chunk_index, attach provenance
      → Evidence[]   ← the only thing retrieval returns
      → [ RAG answering layer — separate phase, separate approval ]
```

**Hybrid, not vector-only.** Certification content is dense with exact
identifiers — "ISO/IEC 27001:2022", "CMMC", "DORA" — where lexical match beats
embeddings, while paraphrased questions need semantic match. PostgreSQL supplies
both with no new component. `english` FTS is safe: single-language site.
**CONFIRMED**

**The answerability policy — OPEN-2 RESOLVED.** Preston answers **only** when
retrieved canonical evidence supports the answer and the question is within the
approved InterCert knowledge domain. It must not guess, invent missing facts,
expose confidential or internal data, or answer an unsupported question as though
evidence existed. **Insufficient or conflicting evidence → refusal, redirect, or
an explicit uncertainty response.** Formal thresholds belong to retrieval and
evaluation (Phase 6.6), not to the canonical foundation.

**The filter is SQL, never a prompt.** Answerability is not a prompt-only access
control: retrieval filters and evidence quality are enforced structurally, and
the LLM *explains* evidence rather than creating it. Archived, boilerplate and
non-answerable content is excluded by the query predicate. A prompt instruction
is not an access control. **RECOMMENDED**

**`Evidence` carries its own citation** — `chunk_id`, `document_id`, verbatim
`text`, `heading_path`, `canonical_uri`, `title`, `content_type`, `retrieved_at`,
`published_at`, `score`, `rank`. Citations are assembled from stored provenance,
so the answering layer can never invent one. The boundary is structural:
`blocks`/`content` are always source text; `metadata.llm.*` is always generated.

---

## 9. What happens when things fail

**The governing rule: failure never destroys knowledge.** Every failure path
leaves the last known-good document `active` and retrievable. There is
deliberately **no `failed` state on a document** — failures are counted, never
applied.

| Failure | Behaviour |
| --- | --- |
| MySQL or API unavailable | Inventory build fails → **abort before touching any document; zero writes**; alert |
| Inventory collapses | Mass-archival gate aborts the run; **nothing archived**; human investigates |
| One record fails to extract | Backoff and retry, then keep last known-good, stay `active`, increment `consecutive_failure_count` |
| Record absent from source | `gone_count++`. Never archives on its own — requires the strike rule *and* the gate |
| Malformed / implausible content | Rejected by the quality gate **before hashing**; last known-good retained |
| Zero chunks produced | Treated as validation failure — roll back, keep previous state |
| Embedding provider down | Chunks persist **without vectors**; document stays `active` and full-text searchable; idempotent backfill completes it later. The assistant degrades rather than going dark |
| Crash mid-run | Per-document commits: N complete, 0 partial. The stale `running` row is closed as `aborted` by the next run's startup sweep |
| Two runs start concurrently | PostgreSQL advisory lock; the second process exits cleanly |
| Our own normalization rule changed | Version mismatch → counted `REPROCESSED`; change-rate alarm suppressed **for that cause only** |
| Bad extractor version corrupted documents | `extractor_version` identifies exactly which to re-derive → targeted `reprocess`, not a full rebuild |

**The dangerous failures are the quiet ones.** Every metric is a query over
`ingestion_runs` and `documents` — no metrics infrastructure is introduced. The
ones that matter most: % of documents reported changed (catches volatile
fragments leaking into the hash, the classic runaway-cost cause), extraction
yield vs each document's own history (catches a source template change silently
breaking extraction), and **archival count as a fraction of the active corpus**
(catches a bad source deploy erasing the KB — this one aborts the run).

---

## 10. Security boundary

- **Read-only, always.** Ingestion issues `SELECT` against MySQL and `GET`
  against the API. It never writes to any source system.
- **Enforcement is structural, not procedural.** Every adapter declares an
  explicit table-and-column allow-list. **No `SELECT *`, no table dump, no
  wildcard endpoint consumption.** A table outside the allow-list is unreachable
  *by construction*, not by review.
- **Never ingested, in any form:** client certificates (17,484), client addresses
  (13,449), training certificates (2,306); all leads and personal submissions
  (contact forms, newsletter subscribers, career forms, feedback); all
  `auth_*`, session, token-blacklist and Django-internal tables.
  `django_admin_log` is retained as a **change signal only, never as corpus**.
- **Credentials** come from `Settings` (`backend/preston/core/config.py`) via
  environment and `.env` only. No connection string, key or token appears in
  code, logs, error bodies or documents. The `DatabaseNotReadyError` pattern in
  `backend/preston/api/v1/health.py` — a fixed generic `detail` so the underlying
  exception can never leak a URL or credential — is the model every adapter
  follows.
- **Injection surface.** Content is company-controlled, so risk is low but not
  zero: HTML comments and hidden text in rich-text fields are stripped at
  extraction; user-generated content is excluded by policy.
- **Open governance item, raised not buried:** the production dump on a developer
  workstation contains client-certificate and contact-enquiry records — **OPEN-5**,
  to be settled with security before further use.

---

## 11. What is NOT used

| Not used | Why |
| --- | --- |
| **The website as a content source** | Its SSR payload is the API response re-served; it holds nothing the API lacks. Probe only |
| **HTTP status as a visibility signal** | Angular returns 200 for every path, including nonexistent ones. Content identity against the per-category fallback signature is the only valid method |
| **Registry / sitemap as a publication filter** | Q1 disproved it — 17 of 18 unregistered records are live, and the sitemap ships 3 dead URLs. Discovery aid only |
| **`NewBlogs.status`** | **Never read.** 77% empty across five encodings, consumed by no code path. Not a publication signal |
| **Timestamps as a change oracle** | 288 of 298 models carry no lifecycle field; content models carry no `updated_at` |
| **Any LLM inside the ingestion path** | Every stage must be deterministic and reproducible. Cleaning, normalization, hashing, rewriting and deciding publication state are **categorically forbidden** for a model. Enrichment is additive and namespaced to `metadata.llm.*`. In retrieval the LLM explains evidence; it never creates it |
| **Legacy `subone_blog` (5 rows) + 12 `blogsec*` tables** | Superseded by `NewBlogs`; ingesting invites contradictory retrieval. **OPEN-8** |
| **CMG placeholder** (`subone_cmgsubpage`, `url_string='a'`) | Placeholder data. Excluded, not deferred |
| **`main/models.py` (283 phantom models)** | Unregistered app, no backing tables. Any mapping derived from it is wrong |
| **`/NewCertificationList_api`, `/NewCertifcation_verfication_api`** | HTTP 500 against tables that do not exist |
| **Three dead routes** (`…/grc/cmcc`, `…/iso-iec-27001-2022-2022`, `…/management-system-training/iso-20121-2012`) | Production-verified to render the not-found shell. Deny-listed at discovery |
| **Browser automation (Playwright)** | The historical `docs/PHASE_6_INGESTION.md` §9 recommended it on the finding that the site is client-rendered. That document is **absent from this repository and from its entire git history**, so nothing can be marked superseded in place. The source contract supersedes the finding: the site is Angular **with SSR** and serves rendered HTML to plain HTTP. With source-first extraction, no browser is needed. Superseded — source-first extraction is current (`docs/PHASE_6_KB_ARCHITECTURE.md` §16) |
| **Separate vector database** | Breaks transactional consistency between text and vectors, adds a store to operate, and solves a capacity problem that does not exist at ~10⁴ chunks |
| **Message broker (Kafka/Celery/Redis), orchestration (Airflow), CDC (Debezium), microservices, agent framework** | One producer, one consumer, once a night. Idempotency already provides retry and resume |
| **The `embedding` column and HNSW index** | **DEFERRED** with **OPEN-3** — dimension is a hard schema commitment; no vector column before that approval |
| **Any dependency added for convenience** | Phase 6.2 shipped with **zero new dependencies** (NFC, SHA-256 and URL parsing are standard library). The HTML5 fragment parser (`html5lib==1.1`) is now approved and installed — see `docs/PHASE_6_B1_PARSER_DECISION.md`. The MySQL driver (`pymysql[rsa]==1.2.0`) is now approved and installed — see `docs/PHASE_6_B2_MYSQL_DRIVER_DECISION.md`. 6.3 still needs a production HTTP client, justified under `AGENTS.md`. The tokenizer stays tied to OPEN-3 |

---

## 12. Decision status — what is frozen, what still blocks

Per the Implementation Decision Record. Engineering and architecture questions
are frozen; anything needing security, infrastructure, content-owner or
embedding-provider approval stays **OPEN** rather than being quietly closed by
engineering.

### Frozen — the flow above reflects these

| # | Item | Decision | Status |
| --- | --- | --- | --- |
| OPEN-1 | Source authority | MySQL + REST API primary; website verifier; registry/sitemap discovery only (§2) | RESOLVED |
| OPEN-2 | Answerability policy | Answer only from supporting evidence; no guessing; insufficient or conflicting evidence → refuse/redirect (§8) | RESOLVED |
| OPEN-9 | `django_admin_log` | Optimization only; never a change oracle, never overrides `content_hash` (§5) | RESOLVED |
| OPEN-11 | Mass archival | 10% threshold, minimum 10 candidates; MySQL 1 strike, API 2 strikes; probe never archives; configurable (§6) | RESOLVED recommendation |
| OPEN-12 | Visibility probe gating | Never gates ingestion, never archives by itself (§2) | RESOLVED |
| OPEN-13 | Route Map | Explicit, versioned; canonical URIs composed from approved routes; dead routes deny-listed. Final table transcribed in 6.3 (§3 ①) | RESOLVED / 6.3 action |
| OPEN-14 | Locations / accreditations granularity | One canonical document per independently answerable public page or entity (§3 ①) | RESOLVED recommendation |
| U9 | Query parameter ordering | Sort by key then value; duplicates preserved as separate entries (§4) | RESOLVED |
| OPEN-19 | Table headers — the corpus marks none with `<th>`; header rows exist only as `<strong>` | **Option C**: explicit `thead`/`th` authoritative; `<strong>` never promoted; the unmarked case recorded as `metadata.cleaning.table_unmarked_header`, which never affects canonical content or `content_hash`. See `docs/PHASE_6_B3_TABLE_HEADER_DECISION.md` | RESOLVED — **re-scoped, not closed** |

### Still open — these gate what can be built

| # | Item | What it holds up | Status |
| --- | --- | --- | --- |
| **OPEN-3** | Embedding provider, model, dimension, tokenizer, no-training terms | Vector column, HNSW index, **final chunk boundaries** (§7) | **OPEN — blocks 6.5** |
| OPEN-4 | Production MySQL read-only route, credentials, replica lag | Live synchronization (§2.1) | **OPEN — not blocking 6.3** |
| OPEN-5 | Dump governance — provenance, authorization, retention | Continued handling of the dev dump (§2.1, §10) | **OPEN — security** |
| OPEN-6 / OPEN-7 | DORA and ISO 20121 canonical ownership | Which representation is canonical. Interim: keep both, linked, never merged (§4) | **OPEN — owner** |
| OPEN-15 | HTML5 fragment parser (✅ RESOLVED — `html5lib==1.1`, see `docs/PHASE_6_B1_PARSER_DECISION.md`), MySQL driver (✅ RESOLVED — `pymysql[rsa]==1.2.0`, see `docs/PHASE_6_B2_MYSQL_DRIVER_DECISION.md`), production HTTP client (`httpx` is dev-only) | Whether the cleaning stage can run at all (§3) | **PARTIALLY OPEN — parser and driver approved; HTTP client still needs dependency approval** |
| OPEN-20 | FAQ question representation — question fields can carry HTML/block markup | Deterministic FAQ normalization (§3) | **DEFERRED to 6.3** |
| OPEN-16 | Golden set — 50–100 questions incl. exact identifiers, FAQ-style, edge cases and **deliberately unanswerable** ones | Retrieval baselines (§8). Not a blocker for extraction | **ACTION — start now** |
| OPEN-8 | Is the legacy `Blog` table retired? (5 documents) | Nothing — currently excluded (§11) | **Not addressed by the decision record**; still open per KB §18, content owner |

### Phase 6.3 entry conditions

6.3 may begin against the controlled snapshot, provided the allow-list is
followed and no live-sync claim is made. It delivers: `SourceRecord` and adapter
interfaces · the approved HTML5 parser and deterministic block construction · the
MySQL blog adapter on explicit table/column allow-lists · the verified Route Map ·
validation gates before hashing · handling for ordered-list starts, tables, FAQ
HTML, hidden/AI-pasted DOM artifacts and inline images · a ~20-record real-data
slice with **human structural review**.

Chunking, embeddings, pgvector, retrieval, RAG, full synchronization, scheduling
and LLM enrichment all remain outside 6.3.

---

## Document control

| | |
| --- | --- |
| **Purpose** | 5-minute operating guide to SOURCE → KB → RETRIEVAL |
| **Not** | The architecture specification — that is `docs/PHASE_6_KB_ARCHITECTURE.md` |
| **Derived from** | `Preston_Source_of_Truth_FINAL` (2026-09-04) · `docs/PHASE_6_KB_ARCHITECTURE.md` (2026-09-07) · `docs/PRESTON_PHASE_6_OPEN_QUESTIONS_DECISIONS.pdf` (Implementation Decision Record) · `docs/archive/phase-history/PHASE_6_2_COMPLETION.md` |
| **Implementation status** | **Built:** Phase 6.2 canonical foundation — schema, canonical URL normalization, the content/hash normalizer split, the version quartet; migration head `b1d7e4a26c58`; no new dependency. Phase 6.3A synchronization foundation; Phase 6.3B-1 cleaning and block construction; the Blog allow-list, pre-clean gate, and the Blog adapter itself (6.3B-2 steps 1–9, `backend/preston/sources/blog.py`), verified read-only against the real controlled MySQL corpus (368 Blogs → 368 SourceRecords, 0 failures). **Not yet performed:** the first real ingestion run (`SourceRecord` → PostgreSQL through the 6.3A sync engine) — PostgreSQL holds no Blog documents. **Designed, not built:** chunking, embeddings, retrieval. Roughly 60–70% of the existing ingestion foundation is reused; what changes is what flows through it |
| **Numbering note** | `OPEN-n` numbers are **not** shared across source documents. OPEN-1…16 here follow `PHASE_6_KB_ARCHITECTURE.md` §18; OPEN-20 and U9 come from the cleaning contract |
| **Phase note** | `AGENTS.md` records Phase 6.2 — Canonical Foundation as complete, next Phase 6.3 — Source Extraction, matching the decision record and the 6.2 completion record |
| **Update trigger** | Resolution of OPEN-3 · approval of OPEN-4/5/15 · a content-owner decision on OPEN-6/7 · a source schema or API change · any change to the architecture document |
