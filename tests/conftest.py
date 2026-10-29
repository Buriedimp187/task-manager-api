"""Shared PostgreSQL integration-test fixture and its safety checks.

Destructive tests may only run against a database that is explicitly named in TEST_POSTGRES_DB,
and only after `refuse_unless_disposable` has approved it.
"""

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from app.config import get_settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEST_DATABASE = os.environ.get("TEST_POSTGRES_DB")
ALLOWED_TABLES = {"alembic_version", "tasks"}
COUNT_STATEMENT = "SELECT count(*) FROM tasks"


def make_config() -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["configure_logger"] = False  # leave Python's logging untouched
    return config


def refuse_unless_disposable(test_database, configured_database, tables, task_count):
    """Raise unless `test_database` is safe to wipe; runs before anything destructive."""
    if not test_database.endswith("_test"):
        raise AssertionError("TEST_POSTGRES_DB must end with _test")
    if test_database == configured_database:
        raise AssertionError("TEST_POSTGRES_DB must not be the configured development database")
    if not set(tables) <= ALLOWED_TABLES:
        raise AssertionError(f"refusing to run: {test_database} contains other tables: {sorted(tables)}")
    if task_count:
        raise AssertionError(f"refusing to run: {test_database} already holds {task_count} task(s)")


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
        task_count = 0
        if "tasks" in tables:
            with engine.connect() as connection:
                task_count = connection.execute(text(COUNT_STATEMENT)).scalar_one()
        refuse_unless_disposable(TEST_DATABASE, configured_database, tables, task_count)

        config = make_config()
        command.downgrade(config, "base")  # start from an empty schema
    except BaseException:
        # Setup was refused or failed: release the connections and the cached settings first.
        engine.dispose()
        get_settings.cache_clear()
        raise
    yield engine, config
    # The checks above proved this database is disposable, so clearing it is safe. The downgrade
    # itself refuses to drop a table that still holds tasks.
    with engine.begin() as connection:
        if inspect(connection).has_table("tasks"):
            connection.execute(text("DELETE FROM tasks"))
    command.downgrade(config, "base")
    engine.dispose()
    get_settings.cache_clear()
