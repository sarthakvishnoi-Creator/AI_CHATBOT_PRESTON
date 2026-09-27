"""Tests for the service-page ingestion composition root — Phase 6.5.

**No ingestion happens here, and none can.** Every test replaces the three
things the entry point reuses — the PostgreSQL engine builder, the MySQL
engine builder and ``synchronize`` — with recorders, so what is asserted is
the *wiring*: which builder was given which configured URL, which adapter
``synchronize`` would receive, and whether every engine is released on
every path out. No engine is constructed, no socket is opened and
``preston.sync.synchronize`` is never executed, so no document, chunk or
run row can be written by this module's suite.

The deliberately unreachable ``.invalid`` hosts below are the second
guard: even a wiring mistake that slipped past a recorder could not reach
a real database.
"""

import uuid
from collections.abc import Callable, Coroutine
from typing import Any

import pytest
from pydantic import MySQLDsn, PostgresDsn

from preston import ingest_service_pages
from preston.core.config import Settings
from preston.runs import ConcurrentRunError, RunCounters
from preston.sources.contract import SourceAdapter
from preston.sources.fixed_pages import (
    CORPORATE,
    OFFICE_LOCATIONS,
    PRIVACY_POLICY,
    RESOURCE_PROCESS,
    STANDALONE_FAQ,
    FixedPageAdapter,
    FixedPageFamily,
)
from preston.sources.service_faq import ServiceFaqAdapter
from preston.sources.service_page import (
    AUDIT_ASSESSMENT,
    GRC,
    MANAGEMENT_TRAINING,
    PROFESSIONAL_TRAINING,
    SECURITY_TESTING,
    ServicePageAdapter,
)
from preston.sync import SyncReport

# Unreachable by construction: ``.invalid`` is reserved by RFC 2606 and
# never resolves.
POSTGRES_URL = (
    "postgresql+psycopg://svc_test:secret-postgres-pw@pg.invalid:5432/preston_test"
)
MYSQL_URL = "mysql+pymysql://svc_test:secret-mysql-pw@mysql.invalid:3306/source_test"


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


class Wiring:
    """Replaces the three collaborators the composition root reuses."""

    def __init__(self, *, synchronize_error: Exception | None = None) -> None:
        self.postgres_urls: list[str] = []
        self.source_urls: list[str] = []
        self.engines: list[FakeAsyncEngine] = []
        self.source_engines: list[FakeSourceEngine] = []
        self.adapters: list[object] = []
        self._synchronize_error = synchronize_error

    def build_async_engine(self, database_url: str) -> FakeAsyncEngine:
        self.postgres_urls.append(database_url)
        engine = FakeAsyncEngine()
        self.engines.append(engine)
        return engine

    def build_source_engine(self, database_url: str) -> FakeSourceEngine:
        self.source_urls.append(database_url)
        engine = FakeSourceEngine()
        self.source_engines.append(engine)
        return engine

    async def synchronize(
        self, engine: object, adapter: object, *, mode: str = "sync"
    ) -> SyncReport:
        self.adapters.append(adapter)
        if self._synchronize_error is not None:
            raise self._synchronize_error
        return SyncReport(run_id=uuid.uuid4(), counters=RunCounters())

    def install(self, monkeypatch: pytest.MonkeyPatch) -> "Wiring":
        monkeypatch.setattr(
            ingest_service_pages, "build_async_engine", self.build_async_engine
        )
        monkeypatch.setattr(
            ingest_service_pages, "build_source_engine", self.build_source_engine
        )
        monkeypatch.setattr(ingest_service_pages, "synchronize", self.synchronize)
        return self


# ---------------------------------------------------------------------------
# Management Training wiring
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_management_training_receives_both_configured_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wiring = Wiring().install(monkeypatch)
    configured = settings()

    await ingest_service_pages.run_management_training_ingestion(configured)

    assert wiring.postgres_urls == [str(configured.database_url)]
    assert wiring.source_urls == [str(configured.source_mysql_url)]
    assert "pg.invalid" in wiring.postgres_urls[0]
    assert "mysql.invalid" in wiring.source_urls[0]


@pytest.mark.anyio
async def test_management_training_synchronizes_the_generic_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wiring = Wiring().install(monkeypatch)

    await ingest_service_pages.run_management_training_ingestion(settings())

    (adapter,) = wiring.adapters
    assert isinstance(adapter, ServicePageAdapter)
    assert isinstance(adapter, SourceAdapter)
    assert adapter.spec is MANAGEMENT_TRAINING
    assert adapter.source_scope == "management_training"


@pytest.mark.anyio
async def test_both_engines_are_released_even_when_the_run_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wiring = Wiring(synchronize_error=RuntimeError("boom")).install(monkeypatch)

    with pytest.raises(RuntimeError):
        await ingest_service_pages.run_management_training_ingestion(settings())

    assert [engine.disposed for engine in wiring.engines] == [True]
    assert [engine.disposed for engine in wiring.source_engines] == [True]


@pytest.mark.anyio
async def test_an_unconfigured_database_builds_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wiring = Wiring().install(monkeypatch)

    with pytest.raises(RuntimeError, match="PRESTON_DATABASE_URL"):
        await ingest_service_pages.run_management_training_ingestion(
            Settings(database_url=None, source_mysql_url=MySQLDsn(MYSQL_URL))
        )

    assert wiring.engines == []
    assert wiring.source_engines == []


@pytest.mark.anyio
async def test_an_unconfigured_mysql_source_releases_the_database_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wiring = Wiring().install(monkeypatch)

    with pytest.raises(RuntimeError, match="PRESTON_SOURCE_MYSQL_URL"):
        await ingest_service_pages.run_management_training_ingestion(
            Settings(database_url=PostgresDsn(POSTGRES_URL), source_mysql_url=None)
        )

    assert wiring.engines == []


# ---------------------------------------------------------------------------
# Service FAQ wiring — independent, and needs no MySQL
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_faq_run_needs_no_mysql_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The committed dataset is the source, so a MySQL outage must not
    decide whether the FAQ set is synchronized."""
    wiring = Wiring().install(monkeypatch)

    await ingest_service_pages.run_service_faq_ingestion(
        Settings(database_url=PostgresDsn(POSTGRES_URL), source_mysql_url=None)
    )

    assert wiring.source_urls == []
    (adapter,) = wiring.adapters
    assert isinstance(adapter, ServiceFaqAdapter)
    assert isinstance(adapter, SourceAdapter)
    assert adapter.source_scope == "service_faq"


@pytest.mark.anyio
async def test_the_faq_run_releases_its_engine_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wiring = Wiring(synchronize_error=RuntimeError("boom")).install(monkeypatch)

    with pytest.raises(RuntimeError):
        await ingest_service_pages.run_service_faq_ingestion(settings())

    assert [engine.disposed for engine in wiring.engines] == [True]


# ---------------------------------------------------------------------------
# The two sources are scoped separately and run independently
# ---------------------------------------------------------------------------


def test_the_two_runs_carry_different_reconciliation_scopes() -> None:
    """The property that keeps one source's inventory from ever flagging
    the other's documents — or a Blog document — as missing."""
    scopes = {MANAGEMENT_TRAINING.source_scope, ServiceFaqAdapter().source_scope}
    assert scopes == {"management_training", "service_faq"}
    assert "blog" not in scopes


def test_a_failing_first_source_does_not_withhold_the_second(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``main`` runs each source inside its own boundary."""
    wiring = Wiring().install(monkeypatch)
    monkeypatch.setattr(ingest_service_pages, "get_settings", settings)
    monkeypatch.setattr(
        ingest_service_pages,
        "run_management_training_ingestion",
        _raiser(RuntimeError("source down")),
    )

    assert ingest_service_pages.main() == 1
    (adapter,) = wiring.adapters
    assert isinstance(adapter, ServiceFaqAdapter)


def test_a_held_lock_is_reported_without_being_called_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Wiring().install(monkeypatch)
    monkeypatch.setattr(ingest_service_pages, "get_settings", settings)
    monkeypatch.setattr(
        ingest_service_pages,
        "run_management_training_ingestion",
        _raiser(ConcurrentRunError("Another run is in progress.")),
    )

    assert ingest_service_pages.main() == 2


def test_a_failure_outranks_a_held_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    Wiring().install(monkeypatch)
    monkeypatch.setattr(ingest_service_pages, "get_settings", settings)
    monkeypatch.setattr(
        ingest_service_pages,
        "run_management_training_ingestion",
        _raiser(ConcurrentRunError("Another run is in progress.")),
    )
    monkeypatch.setattr(
        ingest_service_pages,
        "run_service_faq_ingestion",
        _raiser(RuntimeError("boom")),
    )

    assert ingest_service_pages.main() == 1


def _raiser(error: Exception) -> Callable[..., Coroutine[Any, Any, SyncReport]]:
    """Return an async stand-in for a run function that always raises."""

    async def run(_settings: Settings, *, mode: str = "sync") -> SyncReport:
        raise error

    return run


# ---------------------------------------------------------------------------
# GRC wiring — the same generic adapter, a different spec, explicit only
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_grc_synchronizes_the_generic_adapter_with_the_grc_spec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wiring = Wiring().install(monkeypatch)

    await ingest_service_pages.run_grc_ingestion(settings())

    (adapter,) = wiring.adapters
    assert isinstance(adapter, ServicePageAdapter)
    assert isinstance(adapter, SourceAdapter)
    assert adapter.spec is GRC
    assert adapter.source_scope == "grc"
    assert adapter.source_type == "mysql"


@pytest.mark.anyio
async def test_grc_receives_both_configured_urls_and_releases_both_engines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wiring = Wiring().install(monkeypatch)
    configured = settings()

    await ingest_service_pages.run_grc_ingestion(configured)

    assert wiring.postgres_urls == [str(configured.database_url)]
    assert wiring.source_urls == [str(configured.source_mysql_url)]
    assert [engine.disposed for engine in wiring.engines] == [True]
    assert [engine.disposed for engine in wiring.source_engines] == [True]


def test_grc_is_never_run_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare invocation must not ingest a family nobody named."""
    assert "grc" not in ingest_service_pages.DEFAULT_SCOPES
    wiring = Wiring().install(monkeypatch)
    monkeypatch.setattr(ingest_service_pages, "get_settings", settings)

    assert ingest_service_pages.main() == 0

    assert [type(a).__name__ for a in wiring.adapters] == [
        "ServicePageAdapter",
        "ServiceFaqAdapter",
    ]
    assert all(getattr(a, "source_scope", None) != "grc" for a in wiring.adapters)


def test_naming_grc_runs_grc_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Management Training must not be invoked as a side effect of GRC."""
    wiring = Wiring().install(monkeypatch)
    monkeypatch.setattr(ingest_service_pages, "get_settings", settings)

    assert ingest_service_pages.main(["grc"]) == 0

    (adapter,) = wiring.adapters
    assert isinstance(adapter, ServicePageAdapter)
    assert adapter.spec is GRC


def test_an_unknown_scope_is_refused_before_any_engine_is_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wiring = Wiring().install(monkeypatch)
    monkeypatch.setattr(ingest_service_pages, "get_settings", settings)

    assert ingest_service_pages.main(["audit"]) == 1

    assert wiring.engines == []
    assert wiring.source_engines == []
    assert wiring.adapters == []


def test_every_known_scope_carries_its_own_reconciliation_boundary() -> None:
    assert set(ingest_service_pages.SCOPES) == {
        "management_training",
        "grc",
        "audit_assessment",
        "security_testing",
        "resource_process",
        "privacy_policy",
        "corporate",
        "professional_training",
        "standalone_faq",
        "office_locations",
        "service_faq",
    }
    assert "blog" not in ingest_service_pages.SCOPES


# ---------------------------------------------------------------------------
# Audit & Assessment wiring — a third FamilySpec, the same generic adapter
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_audit_assessment_synchronizes_the_generic_adapter_with_its_spec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wiring = Wiring().install(monkeypatch)

    await ingest_service_pages.run_audit_assessment_ingestion(settings())

    (adapter,) = wiring.adapters
    assert isinstance(adapter, ServicePageAdapter)
    assert isinstance(adapter, SourceAdapter)
    assert adapter.spec is AUDIT_ASSESSMENT
    assert adapter.source_scope == "audit_assessment"
    assert adapter.source_type == "mysql"


@pytest.mark.anyio
async def test_audit_assessment_receives_both_configured_urls_and_releases_both_engines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wiring = Wiring().install(monkeypatch)
    configured = settings()

    await ingest_service_pages.run_audit_assessment_ingestion(configured)

    assert wiring.postgres_urls == [str(configured.database_url)]
    assert wiring.source_urls == [str(configured.source_mysql_url)]
    assert [engine.disposed for engine in wiring.engines] == [True]
    assert [engine.disposed for engine in wiring.source_engines] == [True]


def test_audit_assessment_is_never_run_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare invocation must not ingest a family nobody named."""
    assert "audit_assessment" not in ingest_service_pages.DEFAULT_SCOPES
    wiring = Wiring().install(monkeypatch)
    monkeypatch.setattr(ingest_service_pages, "get_settings", settings)

    assert ingest_service_pages.main() == 0

    assert [type(a).__name__ for a in wiring.adapters] == [
        "ServicePageAdapter",
        "ServiceFaqAdapter",
    ]
    assert all(
        getattr(a, "source_scope", None) != "audit_assessment" for a in wiring.adapters
    )


def test_naming_audit_assessment_runs_it_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GRC and Management Training must not be invoked as a side effect."""
    wiring = Wiring().install(monkeypatch)
    monkeypatch.setattr(ingest_service_pages, "get_settings", settings)

    assert ingest_service_pages.main(["audit_assessment"]) == 0

    (adapter,) = wiring.adapters
    assert isinstance(adapter, ServicePageAdapter)
    assert adapter.spec is AUDIT_ASSESSMENT


# ---------------------------------------------------------------------------
# Security Testing wiring — a fourth FamilySpec, the same generic adapter
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_security_testing_synchronizes_the_generic_adapter_with_its_spec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wiring = Wiring().install(monkeypatch)

    await ingest_service_pages.run_security_testing_ingestion(settings())

    (adapter,) = wiring.adapters
    assert isinstance(adapter, ServicePageAdapter)
    assert isinstance(adapter, SourceAdapter)
    assert adapter.spec is SECURITY_TESTING
    assert adapter.source_scope == "security_testing"
    assert adapter.source_type == "mysql"


@pytest.mark.anyio
async def test_security_testing_receives_both_configured_urls_and_releases_both_engines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wiring = Wiring().install(monkeypatch)
    configured = settings()

    await ingest_service_pages.run_security_testing_ingestion(configured)

    assert wiring.postgres_urls == [str(configured.database_url)]
    assert wiring.source_urls == [str(configured.source_mysql_url)]
    assert [engine.disposed for engine in wiring.engines] == [True]
    assert [engine.disposed for engine in wiring.source_engines] == [True]


def test_security_testing_is_never_run_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare invocation must not ingest a family nobody named."""
    assert "security_testing" not in ingest_service_pages.DEFAULT_SCOPES
    wiring = Wiring().install(monkeypatch)
    monkeypatch.setattr(ingest_service_pages, "get_settings", settings)

    assert ingest_service_pages.main() == 0

    assert [type(a).__name__ for a in wiring.adapters] == [
        "ServicePageAdapter",
        "ServiceFaqAdapter",
    ]
    assert all(
        getattr(a, "source_scope", None) != "security_testing" for a in wiring.adapters
    )


def test_naming_security_testing_runs_it_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GRC, Audit & Assessment and Management Training must not be invoked
    as a side effect."""
    wiring = Wiring().install(monkeypatch)
    monkeypatch.setattr(ingest_service_pages, "get_settings", settings)

    assert ingest_service_pages.main(["security_testing"]) == 0

    (adapter,) = wiring.adapters
    assert isinstance(adapter, ServicePageAdapter)
    assert adapter.spec is SECURITY_TESTING


# ---------------------------------------------------------------------------
# Professional Training wiring — a fifth FamilySpec, the same generic adapter
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_professional_training_synchronizes_the_generic_adapter_with_its_spec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wiring = Wiring().install(monkeypatch)
    configured = settings()

    await ingest_service_pages.run_professional_training_ingestion(configured)

    (adapter,) = wiring.adapters
    assert isinstance(adapter, ServicePageAdapter)
    assert isinstance(adapter, SourceAdapter)
    assert adapter.spec is PROFESSIONAL_TRAINING
    assert adapter.source_scope == "professional_training"
    assert wiring.postgres_urls == [str(configured.database_url)]
    assert wiring.source_urls == [str(configured.source_mysql_url)]
    assert [engine.disposed for engine in wiring.engines] == [True]
    assert [engine.disposed for engine in wiring.source_engines] == [True]


def test_professional_training_is_never_run_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert "professional_training" not in ingest_service_pages.DEFAULT_SCOPES
    wiring = Wiring().install(monkeypatch)
    monkeypatch.setattr(ingest_service_pages, "get_settings", settings)

    assert ingest_service_pages.main() == 0

    assert all(
        getattr(a, "source_scope", None) != "professional_training"
        for a in wiring.adapters
    )


def test_naming_professional_training_runs_it_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wiring = Wiring().install(monkeypatch)
    monkeypatch.setattr(ingest_service_pages, "get_settings", settings)

    assert ingest_service_pages.main(["professional_training"]) == 0

    (adapter,) = wiring.adapters
    assert isinstance(adapter, ServicePageAdapter)
    assert adapter.spec is PROFESSIONAL_TRAINING


# ---------------------------------------------------------------------------
# Fixed-route pages — resource_process, privacy_policy, corporate, and the
# standalone_faq and office_locations collections
# ---------------------------------------------------------------------------

FIXED_RUNNERS = [
    ("resource_process", "run_resource_process_ingestion", RESOURCE_PROCESS),
    ("privacy_policy", "run_privacy_policy_ingestion", PRIVACY_POLICY),
    ("corporate", "run_corporate_ingestion", CORPORATE),
    ("standalone_faq", "run_standalone_faq_ingestion", STANDALONE_FAQ),
    ("office_locations", "run_office_locations_ingestion", OFFICE_LOCATIONS),
]


@pytest.mark.anyio
@pytest.mark.parametrize(("scope", "runner", "family"), FIXED_RUNNERS)
async def test_fixed_pages_synchronize_their_own_family_and_release_both_engines(
    monkeypatch: pytest.MonkeyPatch, scope: str, runner: str, family: FixedPageFamily
) -> None:
    wiring = Wiring().install(monkeypatch)
    configured = settings()

    await getattr(ingest_service_pages, runner)(configured)

    (adapter,) = wiring.adapters
    assert isinstance(adapter, FixedPageAdapter)
    assert isinstance(adapter, SourceAdapter)
    assert adapter.family is family
    assert adapter.source_scope == scope
    assert wiring.postgres_urls == [str(configured.database_url)]
    assert wiring.source_urls == [str(configured.source_mysql_url)]
    assert [engine.disposed for engine in wiring.engines] == [True]
    assert [engine.disposed for engine in wiring.source_engines] == [True]


@pytest.mark.parametrize(("scope", "runner", "family"), FIXED_RUNNERS)
def test_fixed_page_scopes_are_never_run_by_default(
    monkeypatch: pytest.MonkeyPatch, scope: str, runner: str, family: FixedPageFamily
) -> None:
    assert scope not in ingest_service_pages.DEFAULT_SCOPES
    wiring = Wiring().install(monkeypatch)
    monkeypatch.setattr(ingest_service_pages, "get_settings", settings)

    assert ingest_service_pages.main() == 0

    assert all(getattr(a, "source_scope", None) != scope for a in wiring.adapters)


@pytest.mark.parametrize(("scope", "runner", "family"), FIXED_RUNNERS)
def test_naming_a_fixed_page_scope_runs_it_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch, scope: str, runner: str, family: FixedPageFamily
) -> None:
    wiring = Wiring().install(monkeypatch)
    monkeypatch.setattr(ingest_service_pages, "get_settings", settings)

    assert ingest_service_pages.main([scope]) == 0

    (adapter,) = wiring.adapters
    assert isinstance(adapter, FixedPageAdapter)
    assert adapter.family is family
