"""Timeouts, connection pool and shutdown: configuration and wiring, without a database."""

import asyncio
import inspect
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import database, main
from app.config import ServerSettings, Settings, get_server_settings, get_settings
from app.main import app

PASSWORD = "test-only-value"


@pytest.fixture
def db_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no .env file in sight
    monkeypatch.setenv("POSTGRES_USER", "app_user")
    monkeypatch.setenv("POSTGRES_PASSWORD", PASSWORD)
    monkeypatch.setenv("POSTGRES_DB", "app_db")
    for name in ("POSTGRES_HOST", "POSTGRES_PORT", "REQUEST_THREADS"):
        monkeypatch.delenv(name, raising=False)
    for name in (
        "DB_STATEMENT_TIMEOUT_MS",
        "DB_LOCK_TIMEOUT_MS",
        "DB_IDLE_IN_TRANSACTION_TIMEOUT_MS",
        "DB_CONNECT_TIMEOUT_SECONDS",
        "DB_POOL_SIZE",
        "DB_MAX_OVERFLOW",
        "DB_POOL_TIMEOUT_SECONDS",
        "DB_POOL_RECYCLE_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    for cached in (get_settings, get_server_settings, database.get_engine, database.get_session_factory):
        cached.cache_clear()
    yield monkeypatch
    for cached in (get_settings, get_server_settings, database.get_engine, database.get_session_factory):
        cached.cache_clear()


# --- settings ------------------------------------------------------------------------------------------------


def test_defaults_are_the_agreed_safe_values(db_env):
    settings = Settings(_env_file=None)

    assert settings.db_statement_timeout_ms == 5_000
    assert settings.db_lock_timeout_ms == 3_000
    assert settings.db_idle_in_transaction_timeout_ms == 15_000
    assert settings.db_connect_timeout_seconds == 5
    assert (settings.db_pool_size, settings.db_max_overflow) == (10, 10)
    assert settings.db_pool_timeout_seconds == 5
    assert settings.db_pool_recycle_seconds == 1_800
    assert ServerSettings(_env_file=None).request_threads == 20


def test_the_default_pool_can_serve_every_request_thread(db_env):
    settings = Settings(_env_file=None)

    assert settings.db_pool_size + settings.db_max_overflow >= ServerSettings(_env_file=None).request_threads


def test_values_are_read_from_the_environment(db_env):
    db_env.setenv("DB_STATEMENT_TIMEOUT_MS", "9000")
    db_env.setenv("DB_LOCK_TIMEOUT_MS", "1000")
    db_env.setenv("DB_IDLE_IN_TRANSACTION_TIMEOUT_MS", "20000")
    db_env.setenv("DB_POOL_SIZE", "4")
    db_env.setenv("DB_MAX_OVERFLOW", "0")
    db_env.setenv("REQUEST_THREADS", "4")

    settings = Settings(_env_file=None)

    assert settings.db_statement_timeout_ms == 9000
    assert settings.db_lock_timeout_ms == 1000
    assert settings.db_idle_in_transaction_timeout_ms == 20000
    assert (settings.db_pool_size, settings.db_max_overflow) == (4, 0)
    assert ServerSettings(_env_file=None).request_threads == 4


def test_the_connection_options_set_exactly_the_three_server_timeouts(db_env):
    db_env.setenv("DB_STATEMENT_TIMEOUT_MS", "7000")

    options = Settings(_env_file=None).connection_options

    assert options == ("-c statement_timeout=7000 -c lock_timeout=3000 -c idle_in_transaction_session_timeout=15000")


@pytest.mark.parametrize(
    "name, value",
    [
        ("DB_STATEMENT_TIMEOUT_MS", "0"),  # 0 would mean "no timeout" in PostgreSQL
        ("DB_STATEMENT_TIMEOUT_MS", "-1"),
        ("DB_STATEMENT_TIMEOUT_MS", "300001"),
        ("DB_LOCK_TIMEOUT_MS", "0"),
        ("DB_IDLE_IN_TRANSACTION_TIMEOUT_MS", "0"),
        ("DB_IDLE_IN_TRANSACTION_TIMEOUT_MS", "3600001"),
        ("DB_CONNECT_TIMEOUT_SECONDS", "0"),
        ("DB_POOL_SIZE", "0"),
        ("DB_POOL_SIZE", "101"),
        ("DB_MAX_OVERFLOW", "-1"),
        ("DB_POOL_TIMEOUT_SECONDS", "0"),
        ("DB_POOL_RECYCLE_SECONDS", "0"),
        ("DB_POOL_SIZE", "ten"),
        ("DB_LOCK_TIMEOUT_MS", "1.5"),
        ("DB_LOCK_TIMEOUT_MS", ""),
    ],
)
def test_invalid_database_values_are_rejected_without_echoing_secrets(db_env, name, value):
    db_env.setenv(name, value)

    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None)

    assert name.lower() in str(error.value)
    assert PASSWORD not in str(error.value)


@pytest.mark.parametrize("value", ["0", "-3", "201", "many", "2.5"])
def test_invalid_request_thread_counts_are_rejected(db_env, value):
    db_env.setenv("REQUEST_THREADS", value)

    with pytest.raises(ValidationError):
        ServerSettings(_env_file=None)


# --- the engine ----------------------------------------------------------------------------------------------


def test_the_engine_uses_the_configured_pool(db_env):
    db_env.setenv("DB_POOL_SIZE", "3")
    db_env.setenv("DB_MAX_OVERFLOW", "2")
    db_env.setenv("DB_POOL_TIMEOUT_SECONDS", "7")
    db_env.setenv("DB_POOL_RECYCLE_SECONDS", "600")

    pool = database.get_engine().pool

    assert (pool.size(), pool._max_overflow, pool._timeout, pool._recycle) == (3, 2, 7, 600)
    assert pool._pre_ping is True


def test_the_engine_passes_the_timeouts_to_every_new_connection(db_env, monkeypatch):
    captured = {}

    def fake_create_engine(url, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr(database, "create_engine", fake_create_engine)
    db_env.setenv("DB_LOCK_TIMEOUT_MS", "2500")

    database.get_engine()

    assert captured["connect_args"] == {
        "connect_timeout": 5,
        "options": "-c statement_timeout=5000 -c lock_timeout=2500 -c idle_in_transaction_session_timeout=15000",
    }


def test_the_engine_is_created_lazily_so_importing_needs_no_database(db_env):
    assert database.get_engine.cache_info().currsize == 0


# --- shutdown ------------------------------------------------------------------------------------------------


def test_dispose_engine_does_nothing_when_no_engine_was_ever_created(db_env, monkeypatch):
    # An unconfigured process must be able to shut down: disposing must not create an engine (which would
    # need the database settings).
    for name in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"):
        monkeypatch.delenv(name)

    database.dispose_engine()

    assert database.get_engine.cache_info().currsize == 0


def test_dispose_engine_closes_the_pool_of_an_existing_engine(db_env, monkeypatch):
    engine = database.get_engine()
    disposed = []
    monkeypatch.setattr(type(engine), "dispose", lambda self, close=True: disposed.append(self))

    database.dispose_engine()

    assert disposed == [engine]


def test_shutdown_disposes_the_engine(db_env, monkeypatch):
    db_env.setenv("SECRET_KEY", "v" * 48)
    calls = []
    monkeypatch.setattr(main, "dispose_engine", lambda: calls.append("disposed"))

    with TestClient(app):
        assert calls == []  # not before the server stops

    assert calls == ["disposed"]


def test_shutdown_disposes_the_engine_even_if_the_server_stops_on_an_error(db_env, monkeypatch):
    db_env.setenv("SECRET_KEY", "v" * 48)
    calls = []
    monkeypatch.setattr(main, "dispose_engine", lambda: calls.append("disposed"))

    with pytest.raises(RuntimeError):
        with TestClient(app):
            raise RuntimeError("stop")

    assert calls == ["disposed"]


def test_startup_sets_the_thread_pool_size(db_env, monkeypatch):
    db_env.setenv("SECRET_KEY", "v" * 48)
    db_env.setenv("REQUEST_THREADS", "7")
    limiter = SimpleNamespace(total_tokens=40)
    monkeypatch.setattr(main.anyio.to_thread, "current_default_thread_limiter", lambda: limiter)

    with TestClient(app):
        assert limiter.total_tokens == 7


def test_startup_still_needs_no_database_settings(db_env):
    db_env.setenv("SECRET_KEY", "v" * 48)
    for name in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"):
        db_env.delenv(name)

    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}


# --- admission: no more requests use the database than the pool has connections ------------------------------


admit = asynccontextmanager(database.admit_request)


def run(coroutine):
    return asyncio.run(coroutine)


def test_get_db_goes_through_the_admission_limiter():
    parameters = inspect.signature(database.get_db).parameters

    assert [p.default.dependency for p in parameters.values()] == [database.admit_request]


def test_requests_up_to_the_pool_capacity_are_admitted_together(db_env):
    db_env.setenv("DB_POOL_SIZE", "2")
    db_env.setenv("DB_MAX_OVERFLOW", "1")

    async def scenario():
        async with admit(), admit(), admit():
            return "all three inside at once"

    assert run(scenario()) == "all three inside at once"


def test_a_request_over_capacity_waits_and_is_admitted_when_a_slot_frees_up(db_env):
    db_env.setenv("DB_POOL_SIZE", "1")
    db_env.setenv("DB_MAX_OVERFLOW", "1")
    db_env.setenv("DB_POOL_TIMEOUT_SECONDS", "5")
    order = []

    async def request(name, hold):
        async with admit():
            order.append(f"{name} in")
            await asyncio.sleep(hold)
        order.append(f"{name} out")

    async def scenario():
        first = asyncio.create_task(request("a", 0.3))
        second = asyncio.create_task(request("b", 0.3))
        await asyncio.sleep(0.05)
        third = asyncio.create_task(request("c", 0))  # capacity is 2: must wait for a or b
        await asyncio.gather(first, second, third)

    run(scenario())

    assert order.index("c in") > min(order.index("a out"), order.index("b out"))
    assert order[:2] == ["a in", "b in"]


def test_waiting_longer_than_the_pool_timeout_is_database_busy(db_env):
    db_env.setenv("DB_POOL_SIZE", "1")
    db_env.setenv("DB_MAX_OVERFLOW", "0")
    db_env.setenv("DB_POOL_TIMEOUT_SECONDS", "1")

    async def scenario():
        async with admit():
            started = asyncio.get_running_loop().time()
            with pytest.raises(database.DatabaseBusy):
                async with admit():
                    pass
            return asyncio.get_running_loop().time() - started

    assert 0.9 < run(scenario()) < 3


def test_a_slot_is_released_when_the_request_fails(db_env):
    db_env.setenv("DB_POOL_SIZE", "1")
    db_env.setenv("DB_MAX_OVERFLOW", "0")
    db_env.setenv("DB_POOL_TIMEOUT_SECONDS", "1")

    async def scenario():
        with pytest.raises(RuntimeError):
            async with admit():
                raise RuntimeError("handler blew up")
        async with admit():  # would time out if the failed request had kept its slot
            return "admitted"

    assert run(scenario()) == "admitted"


def test_a_timed_out_wait_does_not_leak_a_slot(db_env):
    db_env.setenv("DB_POOL_SIZE", "1")
    db_env.setenv("DB_MAX_OVERFLOW", "0")
    db_env.setenv("DB_POOL_TIMEOUT_SECONDS", "1")

    async def scenario():
        async with admit():
            for _ in range(2):
                with pytest.raises(database.DatabaseBusy):
                    async with admit():
                        pass
        async with admit():
            return "admitted"

    assert run(scenario()) == "admitted"


def test_a_saturated_database_is_a_retryable_503_with_a_safe_body():
    response = run(main.database_busy(None, database.DatabaseBusy()))

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert response.body == b'{"detail":"Database busy, retry shortly"}'
