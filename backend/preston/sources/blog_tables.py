"""The blog extraction allow-list — decision record B4.

Declares the only two MySQL tables, and the only columns within them, that
Preston is permitted to read. Everything the source holds that is not named
here is unreachable by construction: there is no reflection, no
``autoload_with``, no ORM mapping, and no ``SELECT *`` anywhere in the
extraction path, so a query can only name a column that appears below.

That matters because the source database holds 401 tables, of which ten —
client certificates, addresses, training certificates, contact and career
forms, feedback, subscribers, staff accounts — carry personal data that
must never enter the knowledge base. An allow-list makes their exclusion a
property of the code rather than a rule someone has to remember.

**Attached to** ``preston.sources.mysql.SOURCE_METADATA``, never to
``preston.core.db.Base.metadata``. The separation is load-bearing: if a
source table were ever declared on ``Base``, Alembic autogenerate could
propose creating it in PostgreSQL. Binding these declarations to a
standalone ``MetaData`` makes that structurally impossible.

**These declarations are read-only descriptions, never DDL.** Nothing in
Preston creates, alters or drops a source table; ``SOURCE_METADATA`` is
never passed to ``create_all``. The column types mirror the live schema so
that what the code claims and what the database holds can be compared by
reading, not by guessing.

Columns deliberately absent, and why:

* ``blog_description``, ``blog_heading``, ``blog_id``, ``blog_name`` — a
  superseded model revision; at most 12 of 368 rows carry a value.
* ``og_title`` — present in the schema, populated in zero of 379 rows.
* ``robots``, ``googlebot``, ``twitter_card``, ``google_site_verification``
  and the remaining ``og_*``/``twitter_*`` fields — crawler directives and
  social-card plumbing, not authored knowledge.
* every column of the other 399 tables.
"""

from sqlalchemy import BigInteger, Column, Date, String, Table
from sqlalchemy.dialects.mysql import LONGTEXT

from preston.sources.mysql import SOURCE_METADATA

#: The blog body and its denormalised FAQ columns (Source of Truth §04:
#: flat, slug-keyed, self-contained in one row).
NEWBLOGS = Table(
    "subone_newblogs",
    SOURCE_METADATA,
    # Identity. ``id`` is the stable native key behind ``source_ref``;
    # ``url`` is the authored slug from which ``canonical_uri`` is composed.
    Column("id", BigInteger, primary_key=True),
    Column("url", LONGTEXT),
    # Authored content.
    Column("title", LONGTEXT),
    Column("long_description", LONGTEXT),
    Column("short_description", LONGTEXT),
    # Page-level SEO text, carried as metadata rather than as body content.
    Column("page_title", LONGTEXT),
    Column("page_description", LONGTEXT),
    # Publication date as authored. A ``date``, not a timestamp, and never
    # a substitute for ``retrieved_at`` or for change detection.
    Column("posted_date", Date),
    # A relative media key such as ``images/ISO37001_blog1.png`` — carried
    # verbatim as an asset reference and never fetched or resolved.
    Column("service_image", String(100), nullable=False),
    # Recorded as metadata only. It is not a publication gate: the column
    # holds ``''`` (284), ``'1'`` (57), ``'1.0'`` (24), ``'0'`` (2) and
    # ``'0.0'`` (1), and filtering on it would discard most of the corpus.
    Column("status", LONGTEXT),
    # Join key to the SEO metadata row. Nullable: 4 blogs have none, which
    # is why the adapter's join is a LEFT JOIN.
    Column("blog_meta_id", BigInteger),
    # The denormalised FAQ pairs — five fixed slots, each half nullable.
    # A slot with only one half populated is a half pair and is discarded
    # during block construction rather than stored.
    Column("faq_question1", LONGTEXT),
    Column("faq_question2", LONGTEXT),
    Column("faq_question3", LONGTEXT),
    Column("faq_question4", LONGTEXT),
    Column("faq_question5", LONGTEXT),
    Column("faq_answer1", LONGTEXT),
    Column("faq_answer2", LONGTEXT),
    Column("faq_answer3", LONGTEXT),
    Column("faq_answer4", LONGTEXT),
    Column("faq_answer5", LONGTEXT),
)

#: Per-blog SEO metadata, reached only through ``newblogs.blog_meta_id``.
#: Nothing here is authoritative content: it lands in
#: ``documents.metadata`` and is never hashed.
BLOGMETA = Table(
    "subone_blogmeta",
    SOURCE_METADATA,
    Column("id", BigInteger, primary_key=True),
    Column("title", String(255), nullable=False),
    Column("description", LONGTEXT, nullable=False),
    Column("keywords", LONGTEXT),
    Column("author", String(100)),
    Column("language", String(20), nullable=False),
)
