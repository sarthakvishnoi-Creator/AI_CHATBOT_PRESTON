# FLOW.md — Preston AI Knowledge Base Flow

> FLOW.md describes how the KB operates. It is not the detailed architecture
> specification. If the architecture changes, this flow must be updated.

**Reads:** `Preston_Source_of_Truth_FINAL` (2026-09-04) ·
`docs/PHASE_6_KB_ARCHITECTURE.md` (2026-09-07; accepted, partially
implemented — 6.2, B1, B2, B3, B4, 6.3A, 6.3B-1 and the Blog adapter
(6.3B-2 steps 1–9) complete; Blog ingestion into PostgreSQL and the FAQ
atomic-chunking reprocess also complete) ·
`docs/PRESTON_PHASE_6_OPEN_QUESTIONS_DECISIONS.pdf` — the Implementation
Decision Record, which freezes the decisions needed to enter Phase 6.3 ·
`docs/archive/phase-history/PHASE_6_2_COMPLETION.md` (Phase 6.2, implemented) ·
`docs/PHASE_6_5_SOURCE_AUTHORITY_DECISION.md` (2026-09-21; amends OPEN-1 for
the five service-page families and freezes per-page FAQ-widget treatment)

**Labels used below, carried from the source documents:**
**CONFIRMED** (evidenced by the source contract or existing code) ·
**RECOMMENDED** (architectural judgment) · **OPEN** (needs approval or evidence) ·
**RESOLVED** (frozen by the decision record)

**Where the flow stands:** stages ①–⑤ up to the canonical seam are **built**
(Phase 6.2 — schema, URL normalization, the content/hash normalizer split, the
version quartet; current migration head `54b5ec347e84`). Cleaning and block
construction (6.3B-1) and the synchronization foundation (6.3A) are built too,
and so are the source adapters — the Blog adapter, the generic service-page
adapter (one class, five `FamilySpec` values), the fixed-route page adapter,
the frontend FAQ adapter and the image-description adapter. The KB now holds
**541 documents and 6,973 chunks** across 13 `source_scope`s, every FAQ pair
held whole as one chunk — see §13 for per-scope status and the 2026-09-22
integrity audit. The **embedding foundation and the retrieval layer are built**
(Phases 7A–7C and 8A–8D; §7, §8): the vector column, the `Embedder`, exact
vector search and the office lookup. **No chunk has been embedded yet** — the
production backfill, and the live validation that follows it, are pending.
Chunking beyond the FAQ rule (the heading tree and token budget) remains
designed, not built. §12 says which decisions are frozen and which still block.

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
   ⑩ EMBED       separate backfill job (not yet run)         │
                    ▼                                        │
   ⑪ PERSIST     one transaction per document ───────────────┤
                    ▼                                        │
 ┌──────────────────────────────────────────────────────┐    │
 │ PostgreSQL + pgvector                                │◄───┘
 │ documents · document_chunks · ingestion_runs         │  metadata.visibility.*
 └───────────────────────────┬──────────────────────────┘
                             ▼
   RETRIEVAL   query → exact vector search (public scopes) → group → Evidence[]
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
| **MySQL** (`ICWebDatabase`) | System of record. Every content byte originates here. Primary extraction for flat, slug-keyed content: blogs (402 ingested), blog-embedded FAQ pairs (478 ingested), blog SEO metadata. **Also primary, as of Phase 6.5, for the five service-page families**: GRC (38, ingested), audit (13, ingested), STC (14, ingested), management-system training (19, ingested), professional training (13, ingested). Also the source for the fixed-route pages (Resource/Process, Privacy Policy, About, the standalone FAQ collection and office locations — `backend/preston/sources/fixed_pages.py`, ingested) — see `docs/PHASE_6_5_SOURCE_AUTHORITY_DECISION.md` and §13 | **Yes** | CONFIRMED |
| **REST API** (DRF, ~90 public endpoints) | Remains primary for standalone FAQ, because `/faqs/` applies `is_active=True` — the single real publication gate in the stack. **For the five service-page families, the API is now validation/fidelity cross-check only** (Phase 6.5 amendment — confirmed, by direct source and live-endpoint inspection, to be an unfiltered passthrough serialization of the same MySQL rows, with no publication gate, filter, or composition logic of its own) | Cross-check only for service pages; **Yes** for standalone FAQ | CONFIRMED |
| **Frontend service FAQ** (`src/app/constants/faq-data.ts`, Angular) | **New in Phase 6.5.** Per-page FAQ widgets on Management Training/GRC/Audit/Security-Testing pages are hardcoded in frontend source, not MySQL or the API. Explicit, temporary, isolated extraction source (`source_scope = "service_faq"`), reachable public entries only — see `docs/PHASE_6_5_SOURCE_AUTHORITY_DECISION.md` §2.2 | **Yes**, for this one content type only | CONFIRMED |
| **Image descriptions** (`backend/preston/sources/image_descriptions_data.json`) | Reviewed text transcribed offline by a vision model from 26 service-page diagrams, exported by `scripts/export_image_descriptions.py` with each image's SHA-256. Ingested as `source_scope = "image_descriptions"`, one document per image, identity `preston-image://intercert/<image_key>`. Preston itself reads only the committed dataset and calls no model. 2 images carry a `confirmed` service-page mapping, 24 `unresolved` (`metadata.source.mapping`) | **Yes** | CONFIRMED |
| **Website probe** (`www.intercert.com`) | **Verifier, never an extractor.** Emits a `VisibilityVerdict` into `metadata.visibility.*` only. Judges by *content identity* (distinct `<title>` + byte size diverging from the per-category fallback signature), **never by HTTP status** — Angular returns 200 for every path. **Advisory metadata only: a failed or unusual probe never blocks valid source extraction and can never archive a document by itself** (OPEN-12 RESOLVED) | **No** | CONFIRMED |
| **Registry / sitemap** (`subone_search` 89, `servicenav` 67, `nav` 85) | **Discovery aid and SEO reconciliation only. Never corpus, never a filter.** Q1 disproved it as a publication gate: 17 of 18 unregistered records are publicly live, and production's own sitemap ships 3 dead URLs | **No** | CONFIRMED |

> **Unresolved discrepancy — standalone FAQ.** The REST API row above still
> records the API as primary for the standalone FAQ. The current
> implementation (`fixed_pages.py`, `source_scope = "standalone_faq"`) reads
> `subone_faq` from MySQL and applies the same `is_active` gate in SQL. No
> decision record amending OPEN-1 for this source was found in `docs/`.

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

> **Phase 6.5 amendment to OPEN-1** (`docs/PHASE_6_5_SOURCE_AUTHORITY_DECISION.md`,
> 2026-09-21). For the five service-page families specifically — GRC, audit,
> STC, management-system training, professional training — **MySQL is now the
> primary extraction source and the REST API is validation/fidelity
> cross-check only**, reversing the API-primary half of OPEN-1's original
> resolution for these families alone. Confirmed by direct inspection of the
> actual Django views/serializers and a live production diff, not by
> documentation alone: every relevant endpoint is an unfiltered passthrough
> of the same MySQL rows, with no publication gate or composition logic of
> its own. Blog and the generic `/faqs/` endpoint are unaffected. This
> amendment also freezes a new, separate decision: per-page FAQ widgets for
> these families are hardcoded in Angular frontend source
> (`src/app/constants/faq-data.ts`), not MySQL or the API, and are ingested
> as an isolated, temporary `source_scope = "service_faq"` content type.

### 2.1 Development snapshot vs. production source — read this before trusting a run

| | Today (development) | Future (production) |
| --- | --- | --- |
| **MySQL** | Local **controlled snapshot `website_db_controlled`** on `127.0.0.1:3307`, read-only user, reached only through `build_source_engine` (which sets `SET SESSION TRANSACTION READ ONLY` on every connection). A point-in-time copy, promoted 2026-09-17. Frozen. Never re-reads. Whether production has since diverged is **UNKNOWN** | Read-only access to prod RDS `ICWebDatabase` or a replica — route and lag **OPEN-4** |
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

**Current chunking** (`build_chunks`, `backend/preston/ingestion.py`,
`CHUNKER_VERSION` 3): an `FaqPair` is one chunk, whole, never windowed and
never merged with a neighbour. Every other run of blocks is joined and cut by
the character window `chunk_text` — 1,000 characters with 150 overlap, which
can cut mid-sentence. Image alt text listed in `NON_KNOWLEDGE_ALT_TEXT`
contributes no chunk text. Each chunk row carries `document_id`,
`chunk_index`, `content`, **`content_hash`** (SHA-256 of the exact chunk text,
written whenever chunks are rebuilt), and the nullable `embedding` and
`embedding_model` columns; no token count or heading path is stored.
`chunker_version` is stored on each document and compared, so a chunker-only
change reprocesses. A rebuild deletes and reinserts a document's chunks with
no embedding, so a changed or reprocessed document's vectors are rebuilt by the
backfill. The **chunk text is also the text that is embedded**: a Phase 7
experiment (evaluation-only, `scripts/evaluate_chunk_context.py`) tested
prepending `{title} › {heading path}` and kept the existing representation.
Apart from that, the rest of §7 describing structure-aware chunking is the
**planned** design, except the FAQ rule.

**Chunk (planned)** — deterministic and structure-aware, for a fixed
`(chunker_version, tokenizer_id)`:

- Primary split is the **heading tree**; each leaf section is a candidate chunk.
- Target ~400 tokens, hard max ~800, measured with the embedding model's own tokenizer.
- Undersized sections merge with siblings under the same parent, never across parents.
- **FAQ pairs are one chunk each, question and answer never separated.**
- Lists never split mid-item; tables never split (oversized → row groups repeating the header **where one exists** — no table in the current corpus marks a header, so there is nothing to repeat; contract §10.1.2, B3).
- A title-and-heading context prefix was **evaluated and not adopted**: on Golden Set v2 it lowered first-result accuracy (R@1) and helped only some other cases, so it did not justify re-embedding on a different representation. Chunk text, and so the chunk hash, stay as stored.
- Identity is **both**: positional `(document_id, chunk_index)` for order and citation, semantic `content_hash` for reuse (the hash is stored since Phase 7A).
- A document producing zero chunks is a validation failure, not a valid empty document — roll back, keep the previous state.

**Embed** — the provider side is **built**: the `Embedder` protocol and the
OpenAI implementation (`backend/preston/embedding.py`), which validates every
response and never returns a vector of the wrong size. The **backfill is not
built or run**: it will be a separate job, not part of ingestion, so ingestion
stays free of any provider call and a provider outage can never block a text
update. Planned reuse is keyed on **chunk `content_hash` as a multiset**, never
on chunk index (index-keyed reuse silently pairs a vector with different text
when content shifts); a model change yields zero reuse and a correct full
re-embed. **RECOMMENDED**

**Persist** — text persistence is implemented as described. Vectors will live
beside the text in the same PostgreSQL table (`document_chunks.embedding`,
nullable until backfilled; `embedding` and `embedding_model` are null together
or set together). pgvector is enabled (revision `0d2a9b650819`, image
`pgvector/pgvector:0.8.6-pg18-trixie`). **One transaction per document**, so a
crash leaves N complete documents and 0 partial ones. Search is **exact**
cosine; there is no vector index. No separate vector store at ~10⁴ chunks.
**CONFIRMED / RECOMMENDED**

> **OPEN-3 — RESOLVED (Phase 7):** OpenAI `text-embedding-3-large` at 3,072
> dimensions, stored as `halfvec(3072)` because pgvector indexes plain `vector`
> only to 2,000 dimensions; embedding identity
> `openai:text-embedding-3-large:3072`. **Phase 7A built the storage**
> (migration `54b5ec347e84`): `document_chunks.content_hash`, `embedding`
> and `embedding_model`. **No embeddings have been generated yet** (production
> has 0). Search is **exact**; the HNSW index is **DEFERRED** (future baseline
> `m=16`, `ef_construction=64`). The backfill and the multiset reuse above
> remain designed, not built.

---

## 8. How knowledge reaches retrieval

Retrieval is `backend/preston/retrieval.py` (Phases 8A–8D). It has two entry
points, both read-only, both returning `Evidence`:

```
search_knowledge(session, embedder, query, *, embedding_model, scopes=None, documents=8)
  query → validate (not empty, ≤ 2,000 characters)
        → embed with the configured Embedder (one finite 3,072-dimension vector)
        → exact pgvector cosine search, nearest 50 chunks, over chunks that are
          ● in an explicit PUBLIC_SCOPES allow-list (13 scopes, listed one by one)
          ● in an active document
          ● embedded (not NULL) with the requested embedding_model
        → group by document, keep up to 2 best chunks per document
        → order documents by their best score (ties broken by chunk hash)
        → Evidence[]

get_office_locations(session)
  → one structured read of the active office_locations document
  → its 3 stored chunks as Evidence (no query, no embedding, no per-office parsing)
```

`search_knowledge` has no lanes, no source quotas, no full-text search, no rank
fusion, no reranking and no vector index; it makes no answerability decision
and calls no LLM. `get_office_locations` needs no embeddings, so it works before
the backfill. **CONFIRMED** (built and tested).

**Vector-only, for now.** The documented plan was full-text + vector fused by
Reciprocal Rank Fusion. Prototype measurements on Golden Set v1 did not support
it: a match-any-word full-text query matched about half the corpus and lowered
first-result accuracy when fused, and a match-all-words query matched nothing
for most natural-language questions. Identifiers such as "42001" or "PCI" occur
in hundreds of chunks (mostly blog), so exact-term matching does not single out
a service page. Full-text search and fusion are **deferred**, not rejected: revisit with a
narrow identifier strategy if Golden Set v2 shows exact-identifier misses. **RECOMMENDED**

**Source competition.** Blog chunks are about 93% of the corpus and can outrank
INTERCERT's own pages for the same topic. The offline lane experiment (8B)
quantified this; `retrieval.py` still holds the lane helpers (`LANES`,
`RetrievalLimits`, `select`) and the 8B evaluator uses them, but **production
search does not**. Whether results should ever be presented per source is a
decision for the orchestration layer.

**The answerability policy — OPEN-2 RESOLVED.** Preston answers **only** when
retrieved canonical evidence supports the answer and the question is within the
approved InterCert knowledge domain. It must not guess, invent missing facts,
expose confidential or internal data, or answer an unsupported question as though
evidence existed. **Insufficient or conflicting evidence → refusal, redirect, or
an explicit uncertainty response.** **Retrieval does not make this decision.**
Measured top similarity scores for unanswerable questions (0.27–0.71) overlap
those of answerable ones, so a threshold would be invented, not measured.
Weak evidence is returned with its score; deciding whether it suffices belongs
to the later orchestration layer.

**The filter is SQL, never a prompt.** Visibility is enforced structurally in
the query: only listed public scopes, only active documents, only chunks
embedded with the requested model. An archived or unlisted source can never
reach a caller, and a scope added to the database later stays excluded until it
is deliberately added to `PUBLIC_SCOPES`. A prompt instruction is not an access
control. **CONFIRMED**

**`Evidence` carries its own provenance** — `text`, `chunk_content_hash`,
`canonical_uri`, `title`, `source_scope`, `content_type`, `chunk_index`,
`retrieval_method` (`vector` or `structured`), `rank` (position in the returned
list) and `citation_uri`, plus `score` and `embedding_model` for vector results
(a score is meaningless without its model) and an optional
`document_content_hash`. Identity is the chunk hash plus the canonical URI, never
a row id: chunk ids are regenerated on every rebuild. `citation_uri` is the
canonical URI, except for image descriptions, which cite their parent service
page when the mapping is confirmed and cite nothing otherwise, so a
`preston-image://` identity never reaches a user. Raw `metadata`, internal ids,
source-system identifiers and vision-model output are never carried.

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
| Embedding provider down | Ingestion never calls the provider, so text updates are unaffected: chunks persist **without vectors** and the document stays `active`; the (planned, idempotent) backfill completes them later. `search_knowledge` raises a typed `RetrievalError` (provider text is never passed on); `get_office_locations` still works |
| Search finds no embedded chunks for the requested model | `search_knowledge` raises `RetrievalError` rather than returning an empty list that would look like "nothing matched". Partial coverage searches what exists; no match returns `[]` |
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
| **The HNSW index** | **DEFERRED** — at ~7,000 chunks exact search is fast and has perfect recall. The `embedding halfvec(3072)` column itself exists since Phase 7A |
| **Full-text search and rank fusion (RRF)** | **DEFERRED** — prototype measurements did not support them (§8). No `tsvector` column or full-text index exists |
| **Lanes, source quotas, reranking in production search** | Not used. The lane helpers remain for offline evaluation only (§8) |
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
| **OPEN-3** | Embedding provider, model, dimension, tokenizer, no-training terms | Vector column, HNSW index, **final chunk boundaries** (§7) | **RESOLVED** — OpenAI `text-embedding-3-large`, `halfvec(3072)` (§7) |
| OPEN-4 | Production MySQL read-only route, credentials, replica lag | Live synchronization (§2.1) | **OPEN — not blocking 6.3** |
| OPEN-5 | Dump governance — provenance, authorization, retention | Continued handling of the dev dump (§2.1, §10) | **OPEN — security** |
| OPEN-6 / OPEN-7 | DORA and ISO 20121 canonical ownership | Which representation is canonical. Interim: keep both, linked, never merged (§4) | **OPEN — owner** |
| OPEN-15 | HTML5 fragment parser (✅ RESOLVED — `html5lib==1.1`, see `docs/PHASE_6_B1_PARSER_DECISION.md`), MySQL driver (✅ RESOLVED — `pymysql[rsa]==1.2.0`, see `docs/PHASE_6_B2_MYSQL_DRIVER_DECISION.md`), production HTTP client (`httpx` is dev-only) | Whether the cleaning stage can run at all (§3) | **PARTIALLY OPEN — parser and driver approved; HTTP client still needs dependency approval** |
| OPEN-20 | FAQ question representation — question fields can carry HTML/block markup | Deterministic FAQ normalization (§3) | **DEFERRED to 6.3** |
| OPEN-16 | Golden set — 50–100 questions incl. exact identifiers, FAQ-style, edge cases and **deliberately unanswerable** ones | Retrieval baselines (§8). Not a blocker for extraction | **PARTLY DONE** — Golden Set v2 (`tests/fixtures/golden_retrieval_set.json`: 51 cases, 42 answerable, 9 unanswerable) was authored from the KB and validated against it. **Real, anonymised customer questions are still outstanding** |
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
and LLM enrichment all remained outside 6.3 (historical; embeddings and retrieval
were built later, in Phases 7–8).

---

## 13. Phase 6 / KB ingestion status

The first six scopes below were audited read-only, implemented, reviewed, and
then ingested in exactly one controlled run of their own `source_scope`; the
rest were ingested later, each in runs of its own scope. No run touched more
than one scope. Counts are the current stored state (read-only query,
2026-09-27): **541 documents · 6,973 `document_chunks`**, all `active`;
29 `ingestion_runs`, all `succeeded`.

| Family | `source_scope` | Documents | Chunks | Status |
| --- | --- | --- | --- | --- |
| **Blog** | `blog` | 402 | 6,514 | **Ingested and verified** — audit found no issues; reprocessed under `CHUNKER_VERSION` 3 on 2026-09-23 (REPROCESSED = 402) |
| **Management Training** | `management_training` | 19 | 58 | **Ingested and verified** — audit found no issues |
| **Service FAQ** (frontend `faq-data.ts`) | `service_faq` | 8 | 28 | **Ingested and verified** — audit found no issues |
| **GRC** | `grc` | 38 | 145 | **Ingested and verified** — audit found no issues |
| **Audit & Assessment** | `audit_assessment` | 13 | 56 | **Ingested and verified** — audit found no issues |
| **Security Testing** | `security_testing` | 14 | 49 | **Ingested and verified** — audit found no issues |
| **Professional Training** | `professional_training` | 13 | 14 | **Ingested** 2026-09-23 — not covered by the 2026-09-22 audit; no written audit record in `docs/` |
| **Resource / Process** (fixed route) | `resource_process` | 4 | 19 | **Ingested** 2026-09-23 — not covered by the audit |
| **Privacy Policy** (fixed route) | `privacy_policy` | 1 | 4 | **Ingested** 2026-09-23 — not covered by the audit |
| **About** (fixed route) | `corporate` | 1 | 16 | **Ingested** 2026-09-23 — not covered by the audit |
| **Standalone FAQ** (collection) | `standalone_faq` | 1 | 10 | **Ingested** 2026-09-23 — not covered by the audit |
| **Office locations** (collection) | `office_locations` | 1 | 3 | **Ingested** 2026-09-23 — not covered by the audit |
| **Image descriptions** (vision-derived) | `image_descriptions` | 26 | 57 | **Ingested** 2026-09-27 — not covered by the audit |

Stored `chunker_version` is 3 for `blog` and `image_descriptions` and 2 for
the other 11 scopes, which will therefore report REPROCESSED on their next run
(`CHUNKER_VERSION` is 3; see §7).

### FINAL KB INTEGRITY AUDIT

**Status: PASS** (2026-09-22, read-only; no data, schema, configuration or
source code was modified by the audit).

**Verified baseline:** **494 documents · 6,850 `document_chunks`** — all
`active`, 0 archived, across the six scopes above. 11 `ingestion_runs`, all
`succeeded`, none running, each confined to a single scope.

What the audit checked and found clean:

- **Source ↔ KB reconciliation.** All six scopes reconcile 1:1 against the
  controlled source through each adapter's own inventory path — no identity in
  the source without a document, none in the KB without a source identity.
  Re-extracting all 494 documents reproduced **every stored content hash
  byte-for-byte**, with 0 extraction failures: the KB has not drifted.
- **Document and chunk integrity.** 0 duplicate `canonical_uri`, 0 duplicate
  `source_ref`, 0 null/empty identity or hash fields, 0 zero-block documents,
  0 documents without chunks, 0 orphan chunks, 0 empty chunks, 0 negative or
  duplicate chunk indexes, 0 `chunk_index` gaps. Every document's stored chunk
  set is byte-identical to what `build_chunks` produces from its stored blocks.
- **Canonical blocks.** 26,146 blocks, all of valid types, none malformed or
  empty; every `FaqPair` carries both a question and a rendering answer; all
  **516** FAQ pairs are atomic (one pair, one chunk, 0 split).
- **Placeholder and navigation contamination.** 0 placeholder-only blocks
  (`a`, `aa`, `None`, empty, whitespace). The excluded "Other Services" /
  "Other Offerings" navigation blocks appear **nowhere** in the corpus; the six
  textual matches for "other services" are ordinary blog prose ("…or other
  services to…"), classified as legitimate content.
- **FAQ isolation.** `service_faq` is exactly 8 documents / 28 pairs, all from
  the `FAQ_VALUE` dictionary and linked to their Management Training parents by
  metadata only — never merged into the parent document. Native `sec_qna` pairs
  stay inside their own families (audit 9, STC 1). No `FAQ_TEST`/`FAQ_VALUE`/
  `FAQ_DATA` marker reached any block. GRC and Management Training carry 0 FAQ
  pairs, as designed.
- **Images.** Service-page image references are metadata only (315 across four
  families, including Security Testing's 5 item-level images); 0 image paths
  leaked into block text. Blog `ImageRef` blocks (98) are the Blog HTML
  pipeline's documented behaviour, carrying `src`/`alt`/`caption`/`role` with
  `src` excluded from the hash. No OCR or image-derived text anywhere at the
  time of the audit (image-derived text entered later, as the separate
  `image_descriptions` scope).
- **Cross-family isolation.** Scope, route prefix and `source_ref` table
  partition cleanly by family. The ISO 20121:2012 separation holds: two
  documents, different scopes, different URIs, different `source_ref`s,
  different content hashes — never merged.
- **Schema and environment.** At the audit: head `8fc561d8753e`, 5 migrations, `alembic check`
  reports no drift, pgvector 0.8.6 available, `.env` untouched, no dependency
  change. At the time of this audit **no vector column existed**; the column was
  added later by Phase 7A (§7).
- **Quality gates.** 727 tests passed, `ruff format --check`, `ruff check` and
  `pyright` all clean.

---

## 14. Current phase — PHASES 7–8: EMBEDDING & RETRIEVAL

**Built; the production backfill and live validation are pending.** OPEN-3 is
resolved (§7).

- **7A — schema foundation, complete.** Migration `54b5ec347e84` added
  `document_chunks.content_hash` (filled and verified for all 6,973 existing
  chunks), `embedding halfvec(3072)` and `embedding_model`, and allowed a
  `backfill` run to omit `extractor_version`.
- **Provider and configuration, complete.** The `Embedder` protocol and OpenAI
  implementation; settings default to `text-embedding-3-large`, 3,072 dimensions.
- **Evaluation, complete (evaluation-only).** Golden Set v2 (51 cases: 42
  answerable, 9 unanswerable); Small-vs-Large comparison; the chunk-context
  experiment, whose result is to keep the existing chunk text. The artifacts
  under `evaluation_artifacts/` are gitignored and never feed production.
- **8A–8D — retrieval, complete.** The `Evidence` contract and public-scope
  allow-list (8A), the offline strategy evaluation (8B), `search_knowledge` (8C)
  and `get_office_locations` (8D). §8 describes them.
- **Pending.** The embedding backfill (**production currently holds 0
  embeddings**, so `search_knowledge` raises its no-coverage error until it
  runs), then live validation of Golden Set v2 against real retrieval and a
  measured search latency. The HNSW index, full-text search and rank fusion
  are deferred. No orchestration layer, LLM call or chat endpoint exists.

The remainder of structure-aware chunking (§7) changes chunk text and therefore
chunk hashes, so it re-embeds what it changes.

The 2026-09-22 audit verified the KB on the six scopes it covered. The seven
scopes ingested since (§13) have not been through an equivalent integrity
audit.

---

## Document control

| | |
| --- | --- |
| **Purpose** | 5-minute operating guide to SOURCE → KB → RETRIEVAL |
| **Not** | The architecture specification — that is `docs/PHASE_6_KB_ARCHITECTURE.md` |
| **Derived from** | `Preston_Source_of_Truth_FINAL` (2026-09-04) · `docs/PHASE_6_KB_ARCHITECTURE.md` (2026-09-07) · `docs/PRESTON_PHASE_6_OPEN_QUESTIONS_DECISIONS.pdf` (Implementation Decision Record) · `docs/archive/phase-history/PHASE_6_2_COMPLETION.md` |
| **Implementation status** | **Built:** Phase 6.2 canonical foundation — schema, canonical URL normalization, the content/hash normalizer split, the version quartet. Phase 6.3A synchronization foundation; Phase 6.3B-1 cleaning and block construction; the Blog allow-list, pre-clean gate and Blog adapter (6.3B-2 steps 1–9, `backend/preston/sources/blog.py`); the Phase 6.5 service-page layer — one generic `ServicePageAdapter` driven by a declarative `FamilySpec` per family (`backend/preston/sources/service_page.py`, allow-list in `service_page_tables.py`) plus the frontend FAQ adapter (`service_faq.py`), the fixed-route page adapter (`fixed_pages.py`) and the image-description adapter (`image_descriptions.py`). **Ingested and stored:** 541 documents and 6,973 `document_chunks` across 13 `source_scope`s (§13), all active, at `normalizer_version` 3 / `hash_version` 1; stored `chunker_version` 3 for `blog` and `image_descriptions`, 2 elsewhere (`CHUNKER_VERSION` is 3); `extractor_version` 2 for Blog, 1 for every other adapter. Implemented chunking rules: an `FaqPair` is one chunk, whole, and non-knowledge alt text contributes no chunk text. The six scopes present on 2026-09-22 were verified by the final KB integrity audit — **PASS** (§13). **Also built (Phases 7–8):** the embedding schema (migration head `54b5ec347e84`: chunk `content_hash`, nullable `halfvec(3072)` `embedding`, `embedding_model`), the `Embedder` and OpenAI provider (`embedding.py`), the retrieval contract and tools (`retrieval.py`: `search_knowledge`, `get_office_locations`), Golden Set v2 and the offline evaluators (`scripts/evaluate_*.py`). **Production embeddings: 0 — the backfill has not run.** **Designed, not built:** the rest of chunking (heading tree, token budget), the embedding backfill, the HNSW index, any orchestration or answering layer (§14) |
| **Numbering note** | `OPEN-n` numbers are **not** shared across source documents. OPEN-1…16 here follow `PHASE_6_KB_ARCHITECTURE.md` §18; OPEN-20 and U9 come from the cleaning contract |
| **Phase note** | `AGENTS.md` records Phase 6 (Knowledge Base ingestion) as complete, Phase 7 (embedding foundation and evaluation) and Phase 8A–8D (retrieval) as complete, and the embedding backfill followed by live retrieval validation as the next work |
| **Update trigger** | Resolution of OPEN-3 · approval of OPEN-4/5/15 · a content-owner decision on OPEN-6/7 · a source schema or API change · any change to the architecture document |
