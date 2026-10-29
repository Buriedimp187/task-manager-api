"""Shared fixtures: per-test authentication settings, and the PostgreSQL integration-test fixture.

Destructive tests may only run against a database that is explicitly named in TEST_POSTGRES_DB,
and only after `refuse_unless_disposable` has approved it.
"""

import os
import secrets
from pathlib import Path

import pytest
from alembic.config import Config
from argon2 import PasswordHasher
from sqlalchemy import create_engine, inspect, text

from alembic import command
from app import security
from app.config import get_auth_settings, get_settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEST_DATABASE = os.environ.get("TEST_POSTGRES_DB")
ALLOWED_TABLES = {"alembic_version", "tasks", "users"}
COUNT_STATEMENT = "SELECT count(*) FROM tasks"
USER_COUNT_STATEMENT = "SELECT count(*) FROM users"


@pytest.fixture(autouse=True)
def authentication_settings(monkeypatch):
    """A new random signing key for every test, and a cheap password hasher.

    The hasher is only for speed. A separate test checks the production parameters.
    """
    monkeypatch.setenv("SECRET_KEY", secrets.token_urlsafe(48))
    monkeypatch.setattr(security, "password_hasher", PasswordHasher(time_cost=1, memory_cost=8, parallelism=1))
    get_auth_settings.cache_clear()
    yield
    get_auth_settings.cache_clear()


def make_config() -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["configure_logger"] = False  # leave Python's logging untouched
    return config


def refuse_unless_disposable(test_database, configured_database, tables, task_count, user_count=0):
    """Raise unless `test_database` is safe to wipe; runs before anything destructive."""
    if not test_database.endswith("_test"):
        raise AssertionError("TEST_POSTGRES_DB must end with _test")
    if test_database == configured_database:
        raise AssertionError("TEST_POSTGRES_DB must not be the configured development database")
    if not set(tables) <= ALLOWED_TABLES:
        raise AssertionError(f"refusing to run: {test_database} contains other tables: {sorted(tables)}")
    if task_count:
        raise AssertionError(f"refusing to run: {test_database} already holds {task_count} task(s)")
    if user_count:
        raise AssertionError(f"refusing to run: {test_database} already holds {user_count} user account(s)")


@pytest.fixture
def test_database(monkeypatch):
    """Point the settings at the test database, after refusing anything that could hold real data."""
    if not TEST_DATABASE:
        pytest.skip("set TEST_POSTGRES_DB to a disposable database whose name ends in _test")

    get_settings.cache_clear()
    configured_database = get_settings().postgres_db  # read from the environment or .env
    monkeypatch.setenv("POSTGRES_DB", TEST_DATABASE)
    get_settings.cache_clear()

    engine = create_engine(get_settings().database_url, connect_args={"connect_timeout": 5})
    try:
        tables = set(inspect(engine).get_table_names())
        counts = {}
        with engine.connect() as connection:
            for table, statement in (("tasks", COUNT_STATEMENT), ("users", USER_COUNT_STATEMENT)):
                counts[table] = connection.execute(text(statement)).scalar_one() if table in tables else 0
        refuse_unless_disposable(TEST_DATABASE, configured_database, tables, counts["tasks"], counts["users"])

        config = make_config()
        command.downgrade(config, "base")  # start from an empty schema
    except BaseException:
        # Setup was refused or failed: release the connections and the cached settings first.
        engine.dispose()
        get_settings.cache_clear()
        raise
    yield engine, config
    # The checks above proved this database is disposable, so clearing it is safe. The downgrades
    # themselves refuse to drop tables that still hold rows.
    with engine.begin() as connection:
        existing = inspect(connection).get_table_names()
        for table in ("tasks", "users"):  # tasks first: they reference users
            if table in existing:
                connection.execute(text(f"DELETE FROM {table}"))
    command.downgrade(config, "base")
    engine.dispose()
    get_settings.cache_clear()
