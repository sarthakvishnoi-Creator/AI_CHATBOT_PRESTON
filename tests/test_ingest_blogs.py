"""Tests for the Blog ingestion composition root.

**No ingestion happens here, and none can.** Every test replaces the three
things the entry point reuses — the PostgreSQL engine builder, the MySQL
engine builder and ``synchronize`` — with recorders, so what is asserted
is the *wiring*: which builder was given which configured URL, what
adapter ``synchronize`` would receive, and whether both engines are
released on every path out. No engine is constructed, no socket is
opened, and ``preston.sync.synchronize`` is never executed, so no
document, chunk or run row can be written by this module's suite.

The deliberately unreachable ``.invalid`` hosts below are the second
guard: even a wiring mistake that slipped past a recorder could not reach
a real database.
"""

import importlib
import logging
import pathlib
import subprocess
import sys
import uuid
from collections.abc import Iterator

import pytest
from conftest import LogRecorder
from pydantic import MySQLDsn, PostgresDsn

from preston import ingest_blogs
from preston import sync as sync_module
from preston.core import db as core_db
from preston.core.config import Settings
from preston.runs import ConcurrentRunError, RunCounters
from preston.sources import mysql as source_mysql
from preston.sources.blog import EXTRACTOR_VERSION, BlogAdapter
from preston.sources.contract import SourceAdapter
from preston.sync import SyncReport

# Unreachable by construction: ``.invalid`` is reserved by RFC 2606 and
# never resolves. The passwords are distinctive so a test can assert they
# never appear in log output.
POSTGRES_URL = (
    "postgresql+psycopg://ingest_test:secret-postgres-pw@pg.invalid:5432/preston_test"
)
MYSQL_URL = "mysql+pymysql://ingest_test:secret-mysql-pw@mysql.invalid:3306/source_test"
MYSQL_PASSWORD = "secret-mysql-pw"

INGEST_LOGGER = "preston.ingest_blogs"


def settings() -> Settings:
    """Return settings carrying both configured URLs.

    Passed explicitly rather than read from ``.env``: init arguments take
    precedence over the environment, so these tests never depend on — or
    reach — the developer's real databases.
    """
    return Settings(
        database_url=PostgresDsn(POSTGRES_URL),
        source_mysql_url=MySQLDsn(MYSQL_URL),
    )


class FakeAsyncEngine:
    """Stands in for the PostgreSQL ``AsyncEngine``; records disposal."""

    def __init__(self) -> None:
        self.disposed = False

    async def dispose(self) -> None:
        self.disposed = True


class FakeSourceEngine:
    """Stands in for the MySQL ``Engine``; records disposal."""

    def __init__(self) -> None:
        self.disposed = False

    def dispose(self) -> None:
        self.disposed = True


def report(counters: RunCounters | None = None) -> SyncReport:
    """Return a synchronization report a faked ``synchronize`` can hand back."""
    return SyncReport(run_id=uuid.uuid4(), counters=counters or RunCounters())


class Wiring:
    """Replaces the three collaborators the composition root reuses.

    Records what each was given and returns a fake, so the composition can
    be asserted without an engine, a connection or a run.
    """

    def __init__(
        self,
        *,
        synchronize_error: Exception | None = None,
        source_engine_error: Exception | None = None,
    ) -> None:
        self.postgres_urls: list[str] = []
        self.source_urls: list[str] = []
        self.engines: list[FakeAsyncEngine] = []
        self.source_engines: list[FakeSourceEngine] = []
        self.synchronized_engines: list[object] = []
        self.adapters: list[object] = []
        self.modes: list[str] = []
        self.report = report()
        self._synchronize_error = synchronize_error
        self._source_engine_error = source_engine_error

    def build_async_engine(self, database_url: str) -> FakeAsyncEngine:
        self.postgres_urls.append(database_url)
        engine = FakeAsyncEngine()
        self.engines.append(engine)
        return engine

    def build_source_engine(self, database_url: str) -> FakeSourceEngine:
        self.source_urls.append(database_url)
        if self._source_engine_error is not None:
            raise self._source_engine_error
        engine = FakeSourceEngine()
        self.source_engines.append(engine)
        return engine

    async def synchronize(
        self, engine: object, adapter: object, *, mode: str = "sync"
    ) -> SyncReport:
        self.synchronized_engines.append(engine)
        self.adapters.append(adapter)
        self.modes.append(mode)
        if self._synchronize_error is not None:
            raise self._synchronize_error
        return self.report

    def install(self, monkeypatch: pytest.MonkeyPatch) -> "Wiring":
        """Patch the names the entry point holds, and return self."""
        monkeypatch.setattr(ingest_blogs, "build_async_engine", self.build_async_engine)
        monkeypatch.setattr(
            ingest_blogs, "build_source_engine", self.build_source_engine
        )
        monkeypatch.setattr(ingest_blogs, "synchronize", self.synchronize)
        return self


@pytest.fixture
def ingest_logs() -> Iterator[LogRecorder]:
    """Capture the entry point's log records.

    Attached to the named logger rather than the root logger, because
    ``configure_logging`` replaces the root handlers and would discard a
    root-level capture. The level is pinned to INFO for the duration and
    restored afterwards, so a test that never calls ``configure_logging``
    still sees the summary.
    """
    recorder = LogRecorder()
    logger = logging.getLogger(INGEST_LOGGER)
    previous = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(recorder)
    try:
        yield recorder
    finally:
        logger.removeHandler(recorder)
        logger.setLevel(previous)


# ---------------------------------------------------------------------------
# Configuration is consumed, not hardcoded
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_both_urls_come_from_the_supplied_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each builder receives the URL its own setting carries."""
    wiring = Wiring().install(monkeypatch)
    configured = settings()

    await ingest_blogs.run_blog_ingestion(configured)

    assert wiring.postgres_urls == [str(configured.database_url)]
    assert wiring.source_urls == [str(configured.source_mysql_url)]
    # Proves the value travelled from settings rather than a literal.
    assert "pg.invalid" in wiring.postgres_urls[0]
    assert "mysql.invalid" in wiring.source_urls[0]


@pytest.mark.anyio
async def test_an_unconfigured_database_builds_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing PostgreSQL URL is refused before any engine exists."""
    wiring = Wiring().install(monkeypatch)

    with pytest.raises(RuntimeError, match="PRESTON_DATABASE_URL"):
        await ingest_blogs.run_blog_ingestion(
            Settings(database_url=None, source_mysql_url=MySQLDsn(MYSQL_URL))
        )

    assert wiring.postgres_urls == []
    assert wiring.source_urls == []


@pytest.mark.anyio
async def test_an_unconfigured_source_builds_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing MySQL URL is refused before the PostgreSQL engine is built.

    Both checks run first precisely so a half-configured environment
    never leaves an engine to dispose of.
    """
    wiring = Wiring().install(monkeypatch)

    with pytest.raises(RuntimeError, match="PRESTON_SOURCE_MYSQL_URL"):
        await ingest_blogs.run_blog_ingestion(
            Settings(database_url=PostgresDsn(POSTGRES_URL), source_mysql_url=None)
        )

    assert wiring.postgres_urls == []
    assert wiring.source_urls == []


# ---------------------------------------------------------------------------
# What synchronize would receive
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_synchronize_receives_the_postgres_engine_and_the_default_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The engine handed to ``synchronize`` is the one the builder returned."""
    wiring = Wiring().install(monkeypatch)

    await ingest_blogs.run_blog_ingestion(settings())

    assert wiring.synchronized_engines == list(wiring.engines)
    assert wiring.modes == ["sync"]


@pytest.mark.anyio
async def test_the_run_mode_is_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A caller-chosen mode reaches the engine unchanged."""
    wiring = Wiring().install(monkeypatch)

    await ingest_blogs.run_blog_ingestion(settings(), mode="reprocess")

    assert wiring.modes == ["reprocess"]


@pytest.mark.anyio
async def test_synchronize_receives_a_blog_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The adapter is a real ``BlogAdapter`` carrying its own version."""
    wiring = Wiring().install(monkeypatch)

    await ingest_blogs.run_blog_ingestion(settings())

    (adapter,) = wiring.adapters
    assert isinstance(adapter, BlogAdapter)
    assert adapter.source_type == "mysql"
    assert adapter.extractor_version == EXTRACTOR_VERSION


@pytest.mark.anyio
async def test_the_adapter_is_built_on_the_source_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``BlogAdapter`` is constructed with the read-only MySQL engine.

    Asserted by recording the constructor argument rather than reading the
    adapter's private attribute, so the test does not depend on how the
    adapter stores it.
    """
    wiring = Wiring().install(monkeypatch)
    built_with: list[object] = []

    def record_adapter(engine: object) -> BlogAdapter:
        built_with.append(engine)
        return BlogAdapter(engine)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(ingest_blogs, "BlogAdapter", record_adapter)

    await ingest_blogs.run_blog_ingestion(settings())

    assert built_with == list(wiring.source_engines)


def test_the_blog_adapter_satisfies_the_source_adapter_protocol() -> None:
    """The contract ``synchronize`` requires is met structurally."""
    assert isinstance(BlogAdapter(FakeSourceEngine()), SourceAdapter)  # pyright: ignore[reportArgumentType]


# ---------------------------------------------------------------------------
# Resource lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_both_engines_are_disposed_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A completed run releases PostgreSQL and MySQL alike."""
    wiring = Wiring().install(monkeypatch)

    result = await ingest_blogs.run_blog_ingestion(settings())

    assert result is wiring.report
    assert [engine.disposed for engine in wiring.engines] == [True]
    assert [engine.disposed for engine in wiring.source_engines] == [True]


@pytest.mark.anyio
async def test_both_engines_are_disposed_when_the_run_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure inside ``synchronize`` still releases both engines."""
    wiring = Wiring(synchronize_error=RuntimeError("boom")).install(monkeypatch)

    with pytest.raises(RuntimeError, match="boom"):
        await ingest_blogs.run_blog_ingestion(settings())

    assert [engine.disposed for engine in wiring.engines] == [True]
    assert [engine.disposed for engine in wiring.source_engines] == [True]


@pytest.mark.anyio
async def test_the_postgres_engine_is_disposed_when_the_source_engine_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An engine already built is released even if the next one cannot be."""
    wiring = Wiring(source_engine_error=RuntimeError("no mysql")).install(monkeypatch)

    with pytest.raises(RuntimeError, match="no mysql"):
        await ingest_blogs.run_blog_ingestion(settings())

    assert [engine.disposed for engine in wiring.engines] == [True]
    assert wiring.source_engines == []
    # Nothing was synchronized: the failure preceded the adapter.
    assert wiring.adapters == []


# ---------------------------------------------------------------------------
# The process entry point
# ---------------------------------------------------------------------------


def test_main_consumes_the_application_settings_and_reports_success(
    monkeypatch: pytest.MonkeyPatch, ingest_logs: LogRecorder
) -> None:
    """``main`` reads settings once, runs, summarises, and exits zero."""
    wiring = Wiring().install(monkeypatch)
    configured = settings()
    monkeypatch.setattr(ingest_blogs, "get_settings", lambda: configured)
    wiring.report = report(RunCounters(new=368, inventory_size=368, reconciled=True))

    assert ingest_blogs.main() == 0

    assert wiring.postgres_urls == [str(configured.database_url)]
    assert wiring.source_urls == [str(configured.source_mysql_url)]
    messages = ingest_logs.messages()
    assert any("new=368" in message for message in messages)
    assert any("size=368" in message for message in messages)


def test_main_exits_cleanly_when_another_run_holds_the_lock(
    monkeypatch: pytest.MonkeyPatch, ingest_logs: LogRecorder
) -> None:
    """Overlap is not an error: it is reported and gets its own exit code."""
    error = ConcurrentRunError("Another ingestion run is already in progress.")
    Wiring(synchronize_error=error).install(monkeypatch)
    monkeypatch.setattr(ingest_blogs, "get_settings", settings)

    assert ingest_blogs.main() == 2

    assert any("already in progress" in m for m in ingest_logs.messages())


def test_main_reports_a_failure_without_leaking_the_connection_url(
    monkeypatch: pytest.MonkeyPatch, ingest_logs: LogRecorder
) -> None:
    """A driver exception's message never reaches the operator's console.

    Driver errors routinely embed the connection URL, and this output goes
    to a terminal or a cron mail, so only the exception class name is
    logged — the same rule ``runs.py`` applies to ``abort_reason``.
    """
    leaky = RuntimeError(f"can't connect to {MYSQL_URL}")
    Wiring(synchronize_error=leaky).install(monkeypatch)
    monkeypatch.setattr(ingest_blogs, "get_settings", settings)

    assert ingest_blogs.main() == 1

    output = ingest_logs.formatted()
    assert "RuntimeError" in output
    assert MYSQL_PASSWORD not in output
    assert "mysql.invalid" not in output


# ---------------------------------------------------------------------------
# Nothing runs by accident
# ---------------------------------------------------------------------------


def test_importing_the_entry_point_builds_and_runs_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Executing the module body must not construct or synchronize anything.

    The collaborators are replaced in the modules they are *defined* in,
    not on the entry point, so that re-importing it picks the recorders up
    the way a first import would.
    """
    calls: list[str] = []

    def unexpected_async_engine(database_url: str) -> object:
        calls.append("build_async_engine")
        return object()

    def unexpected_source_engine(database_url: str) -> object:
        calls.append("build_source_engine")
        return object()

    async def unexpected_synchronize(
        engine: object, adapter: object, *, mode: str = "sync"
    ) -> object:
        calls.append("synchronize")
        return object()

    monkeypatch.setattr(core_db, "build_async_engine", unexpected_async_engine)
    monkeypatch.setattr(source_mysql, "build_source_engine", unexpected_source_engine)
    monkeypatch.setattr(sync_module, "synchronize", unexpected_synchronize)
    try:
        importlib.reload(ingest_blogs)
        assert calls == []
    finally:
        # Undo first, then reload again, so the module is left holding the
        # real collaborators rather than these recorders.
        monkeypatch.undo()
        importlib.reload(ingest_blogs)


def test_the_module_only_runs_itself_under_the_main_guard() -> None:
    """``main`` is called unconditionally nowhere in the module body."""
    assert ingest_blogs.__file__ is not None
    source = pathlib.Path(ingest_blogs.__file__).read_text(encoding="utf-8")
    body = [
        line
        for line in source.splitlines()
        if line and not line.startswith((" ", ")", "#", '"', "'"))
    ]
    assert 'if __name__ == "__main__":' in body
    assert not any(line.startswith("main()") for line in body)


def test_starting_the_application_never_reaches_the_entry_point() -> None:
    """Importing and running Preston must not even load this module.

    Checked in a fresh interpreter, because this suite has already
    imported the entry point itself. Running the full lifespan — not just
    the import — is what proves FastAPI startup cannot trigger ingestion.
    """
    probe = (
        "import sys\n"
        "from fastapi.testclient import TestClient\n"
        "from preston.core.config import Settings\n"
        "from preston.main import create_app\n"
        "app = create_app(settings=Settings(database_url=None, "
        "source_mysql_url=None))\n"
        "with TestClient(app) as client:\n"
        "    client.get('/api/v1/health')\n"
        "print('LOADED', 'preston.ingest_blogs' in sys.modules)\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "LOADED False"


def test_a_summary_reports_a_skipped_reconciliation(
    ingest_logs: LogRecorder,
) -> None:
    """An incomplete inventory is surfaced, not swallowed."""
    counters = RunCounters(
        inventory_complete=False,
        inventory_reason="mysql_query_failed",
        reconciliation_skipped_reason="inventory incomplete",
    )

    ingest_blogs.log_summary(report(counters))

    output = ingest_logs.formatted()
    assert "mysql_query_failed" in output
    assert "inventory incomplete" in output


def test_a_summary_carries_no_document_content(ingest_logs: LogRecorder) -> None:
    """Counters and a run id only — never a URI or a title."""
    summary = report(RunCounters(new=1, missing_candidates=2))

    ingest_blogs.log_summary(summary)

    output = ingest_logs.formatted()
    assert str(summary.run_id) in output
    assert "missing_candidates=2" in output
    assert "http" not in output
