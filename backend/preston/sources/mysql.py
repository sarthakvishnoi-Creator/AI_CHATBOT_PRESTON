"""The MySQL source engine — decision record B2.

MySQL is a *source*, never a destination: this module builds the engine
extraction will read from, and nothing here ever writes to it. It is
deliberately separate from ``preston.core.db``, which that module's own
docstring scopes to "PostgreSQL"; keeping the two apart means this
module's ``MetaData`` can never end up on ``preston.core.db.Base`` and
therefore can never be proposed as a target for an Alembic PostgreSQL
migration.

**Approved (decision record B2):** synchronous SQLAlchemy Core over
``mysql+pymysql`` — not async, because extraction is a nightly batch job
with no request-latency budget, and not the ORM, because every table this
engine may touch must be an explicit, allow-listed ``Table`` declaration,
never a mapped class a stray attribute access could reach further than
intended. ``pymysql[rsa]==1.2.0`` was chosen over ``mysqlclient`` (GPL,
no Linux wheels), ``mysql-connector-python`` (Oracle GPL) and the async
drivers ``aiomysql``/``asyncmy`` (no latency budget to spend a dependency
on); see ``docs/PHASE_6_B2_MYSQL_DRIVER_DECISION.md``.

**What this module does not do.** It declares no tables and no
column allow-list: which tables and columns extraction may read is B4,
not this decision. ``SOURCE_METADATA`` below is the boundary those
declarations attach to when B4 is decided — it is empty by design, not
by omission. There is no write path anywhere in this module, and none
should ever be added to it: MySQL is read-only, enforced by the source
account's grant (the real control) and, in software, by
:func:`_enforce_read_only` on every connection (defense in depth, not a
substitute for the grant).
"""

import asyncio
from collections.abc import Callable

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.pool import ConnectionPoolEntry, NullPool
from sqlalchemy.schema import MetaData

from preston.core.errors import PrestonError

# Standalone and empty. Never `preston.core.db.Base.metadata` — that
# separation is what makes it structurally impossible for Alembic
# autogenerate to ever propose creating a MySQL table in PostgreSQL.
# Allow-listed `Table` objects are added here once B4 decides which
# tables and columns extraction may read; none exist yet.
SOURCE_METADATA = MetaData()


class SourceDatabaseError(PrestonError):
    """The MySQL source is unconfigured or unreachable.

    ``detail`` is always a fixed, generic string, following the same
    pattern as ``api.v1.health.DatabaseNotReadyError``: the underlying
    ``pymysql`` or SQLAlchemy exception may carry the connection URL,
    host, user or password, and must never reach a log line, an error
    response, or any other caller-visible surface.
    """

    title = "Service Unavailable"
    status_code = 503


def _enforce_read_only(
    dbapi_connection: DBAPIConnection, connection_record: ConnectionPoolEntry
) -> None:
    """Reject writes at the session level on every new connection.

    Application-side defense in depth, not the enforcement mechanism —
    the actual control is the ``SELECT``-only grant on the MySQL source
    account (decision record B2). This only makes an accidental write
    fail fast, with a server-side error, in case that account is ever
    over-permissioned. Runs once per connection, which with ``NullPool``
    means once per checkout: nothing here is cached across uses.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("SET SESSION TRANSACTION READ ONLY")
    finally:
        cursor.close()


def build_source_engine(database_url: str) -> Engine:
    """Build the synchronous, read-only MySQL source engine.

    * ``NullPool`` — a nightly batch job holds no connections between
      runs; there is nothing to gain from pooling and nothing idle to
      leak.
    * ``pool_pre_ping=True`` — matches
      ``preston.core.db.build_async_engine``: guards against a
      connection that died since it was last used.
    * ``charset=utf8mb4`` — forced onto the URL when not already present,
      so decoding is deterministic regardless of what the configured URL
      supplies.

    Construction does not open a connection; like the PostgreSQL engine,
    SQLAlchemy connects lazily on first use. Read-only enforcement
    (:func:`_enforce_read_only`) is registered on every connection this
    engine ever opens, not just the first.
    """
    url = make_url(database_url)
    if "charset" not in url.query:
        url = url.update_query_dict({"charset": "utf8mb4"})
    engine = create_engine(url, poolclass=NullPool, pool_pre_ping=True)
    event.listen(engine, "connect", _enforce_read_only)
    return engine


async def run_source_unit[T](unit: Callable[[], T]) -> T:
    """Run one complete, self-contained MySQL unit of work off the event loop.

    This is the isolation pattern decision record B2 approves for
    reaching this synchronous engine from async extraction code: a plain
    ``asyncio.to_thread`` wrapper, nothing more. ``unit`` must open its
    own connection (e.g. ``engine.connect()``), do all of its work, and
    return plain data — never a live ``Connection``, ``Cursor``, or ORM
    object. Nothing SQLAlchemy owns may cross the thread boundary this
    creates, because a ``Connection`` used from two threads at once is
    exactly the failure this function exists to prevent.

    No extraction code calls this yet — it is the boundary future
    extraction will use, not a wired pipeline.
    """
    return await asyncio.to_thread(unit)
