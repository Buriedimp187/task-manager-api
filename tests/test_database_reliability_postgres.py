"""Timeouts and the connection pool against a real PostgreSQL, through the application's own engine.

Skipped unless TEST_POSTGRES_DB names a disposable database (see tests/conftest.py).
"""

import time

import pytest
from fakes import bearer_headers
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from alembic import command
from app import database
from app.config import get_settings
from app.database import get_db
from app.main import app

BUSY = {"detail": "Database busy, retry shortly"}


@pytest.fixture
def configured(test_database, monkeypatch):
    """Rebuild the application's engine from the environment, so each test can choose its own limits."""
    engine, config = test_database
    command.upgrade(config, "head")
    with engine.begin() as connection:
        user_id = connection.execute(
            text("INSERT INTO users (username, password_hash) VALUES ('alice', 'not-a-real-hash') RETURNING id")
        ).scalar_one()
        connection.execute(
            text("INSERT INTO tasks (id, owner_id, title) VALUES (1, :owner, 'held')"), {"owner": user_id}
        )

    def use(**limits):
        for name, value in limits.items():
            monkeypatch.setenv(name.upper(), str(value))
        for cached in (get_settings, database.get_engine, database.get_session_factory):
            cached.cache_clear()
        return database.get_engine()

    yield use, TestClient(app, raise_server_exceptions=False, headers=bearer_headers(user_id))
    app.dependency_overrides.clear()
    database.dispose_engine()
    for cached in (database.get_engine, database.get_session_factory):
        cached.cache_clear()


def test_every_connection_carries_the_configured_timeouts(configured):
    use, _ = configured
    engine = use(db_statement_timeout_ms=4321, db_lock_timeout_ms=1234, db_idle_in_transaction_timeout_ms=9876)

    with engine.connect() as connection:
        shown = {
            name: connection.execute(text(f"SHOW {name}")).scalar()
            for name in ("statement_timeout", "lock_timeout", "idle_in_transaction_session_timeout")
        }

    assert shown == {
        "statement_timeout": "4321ms",
        "lock_timeout": "1234ms",
        "idle_in_transaction_session_timeout": "9876ms",
    }


def test_the_defaults_reach_the_server(configured):
    use, _ = configured

    with use().connect() as connection:
        assert connection.execute(text("SHOW statement_timeout")).scalar() == "5s"
        assert connection.execute(text("SHOW lock_timeout")).scalar() == "3s"
        assert connection.execute(text("SHOW idle_in_transaction_session_timeout")).scalar() == "15s"


def test_a_transaction_can_still_choose_its_own_timeout(configured):
    # The defaults are not hard limits: explicit transaction behavior (SET LOCAL) keeps working.
    use, _ = configured
    with use(db_statement_timeout_ms=200).begin() as connection:
        connection.execute(text("SET LOCAL statement_timeout = '3s'"))
        connection.execute(text("SELECT pg_sleep(0.5)"))  # longer than the default, shorter than the override


def test_a_statement_over_the_timeout_is_cancelled_by_the_server(configured):
    use, _ = configured
    engine = use(db_statement_timeout_ms=200)

    started = time.perf_counter()
    with pytest.raises(DBAPIError) as error:
        with engine.connect() as connection:
            connection.execute(text("SELECT pg_sleep(5)"))

    assert error.value.orig.sqlstate == "57014"
    assert time.perf_counter() - started < 3


def test_a_request_waiting_for_a_row_lock_gets_a_retryable_503_after_the_lock_timeout(configured):
    use, client = configured
    engine = use(db_lock_timeout_ms=300)
    holder = engine.connect()
    try:
        holder.execute(text("SELECT id FROM tasks WHERE id = 1 FOR UPDATE"))
        started = time.perf_counter()
        response = client.patch("/tasks/1", json={"title": "x"})
        waited = time.perf_counter() - started
    finally:
        holder.rollback()
        holder.close()

    assert (response.status_code, response.json()) == (503, BUSY)
    assert response.headers["Retry-After"] == "1"
    assert 0.2 < waited < 3
    assert client.patch("/tasks/1", json={"title": "after"}).status_code == 200  # nothing is left stuck


def test_an_exhausted_pool_is_a_retryable_503_and_recovers_when_a_connection_is_returned(configured):
    use, client = configured
    engine = use(db_pool_size=1, db_max_overflow=0, db_pool_timeout_seconds=1)
    occupied = engine.connect()  # the only connection
    try:
        started = time.perf_counter()
        response = client.get("/tasks")
        waited = time.perf_counter() - started
    finally:
        occupied.close()

    assert (response.status_code, response.json()) == (503, BUSY)
    assert response.headers["Retry-After"] == "1"
    assert 0.8 < waited < 4  # waited for the pool timeout, no longer
    assert client.get("/tasks").status_code == 200


def test_a_session_closed_for_idling_in_a_transaction_is_a_503_not_a_500(configured):
    use, client = configured
    use(db_idle_in_transaction_timeout_ms=300)

    def session_that_idles():
        with database.get_session_factory()() as session:
            session.execute(text("SELECT 1"))  # opens a transaction, then the request stalls
            time.sleep(1)
            yield session

    app.dependency_overrides[get_db] = session_that_idles

    response = client.get("/tasks")

    assert response.status_code == 503
    app.dependency_overrides.clear()
    assert client.get("/tasks").status_code == 200  # a fresh connection works


def test_shutdown_closes_every_pooled_connection(configured):
    use, client = configured
    engine = use()
    assert client.get("/tasks").status_code == 200
    assert engine.pool.checkedin() == 1

    database.dispose_engine()

    assert engine.pool.checkedin() == 0
