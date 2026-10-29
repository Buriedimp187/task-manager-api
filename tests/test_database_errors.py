"""How the task routes report database failures: 503 for outages, a generic 500 for everything else."""

import logging
from types import SimpleNamespace

import psycopg
import psycopg.errors
import pytest
from fakes import bearer_headers
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import (
    DataError,
    IntegrityError,
    InterfaceError,
    InvalidRequestError,
    OperationalError,
    ProgrammingError,
)
from sqlalchemy.exc import TimeoutError as SATimeoutError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm.exc import StaleDataError

from app.config import get_settings
from app.database import get_db
from app.main import app, is_connectivity_failure
from app.models import User

SECRET_HOST = "db.internal.example"
SECRET_USER = "svc_account"
SECRET_PASSWORD = "p@ss:w/ord#?%&=+ test-only"
UNAVAILABLE = {"detail": "Database unavailable"}


class FakeDriverError(Exception):
    """Stands in for a psycopg error; `sqlstate` is None for errors raised before the server answers."""

    def __init__(self, sqlstate=None):
        super().__init__(
            f'connection to server at "{SECRET_HOST}" port 5432 failed: FATAL: '
            f'password authentication failed for user "{SECRET_USER}" ({SECRET_PASSWORD})'
        )
        self.sqlstate = sqlstate


def operational_error(sqlstate=None, **kwargs):
    return OperationalError("SELECT 1", {}, FakeDriverError(sqlstate), **kwargs)


class FailingSession:
    """Raises the given error on the first database call after authentication, like a database that fails
    mid-request. The user lookup succeeds (so the request is authenticated) unless `fail_lookup` is set."""

    def __init__(self, error, fail_lookup=False):
        self.error = error
        self.fail_lookup = fail_lookup
        self.calls = []

    def _fail(self, name):
        self.calls.append(name)
        raise self.error

    def add(self, task):
        self.calls.append("add")

    def get(self, model, key):
        if model is User and not self.fail_lookup:
            return SimpleNamespace(id=key)  # authentication succeeds
        self._fail("get")

    def scalar(self, *args, **kwargs):
        self._fail("scalar")

    def scalars(self, *args, **kwargs):
        self._fail("scalars")

    def commit(self):
        self._fail("commit")


@pytest.fixture
def failing():
    def use(error, fail_lookup=False):
        session = FailingSession(error, fail_lookup)
        app.dependency_overrides[get_db] = lambda: session
        return TestClient(app, raise_server_exceptions=False, headers=bearer_headers(1))

    yield use
    app.dependency_overrides.clear()


# Every route that touches the database, to show the mapping is not specific to one handler.
REQUESTS = [
    ("POST", "/tasks", {"json": {"title": "t"}}),
    ("GET", "/tasks", {}),
    ("GET", "/tasks/1", {}),
    ("PATCH", "/tasks/1", {"json": {"title": "t"}}),
    ("DELETE", "/tasks/1", {}),
]


@pytest.mark.parametrize("method, path, kwargs", REQUESTS)
def test_a_connectivity_failure_is_a_generic_503(failing, method, path, kwargs):
    client = failing(operational_error(sqlstate=None))

    response = client.request(method, path, **kwargs)

    assert response.status_code == 503
    assert response.json() == UNAVAILABLE
    for secret in (SECRET_HOST, SECRET_USER, SECRET_PASSWORD, "FATAL", "password"):
        assert secret not in response.text


def test_an_outage_while_checking_the_token_is_also_503(failing):
    client = failing(operational_error(sqlstate=None), fail_lookup=True)

    response = client.get("/tasks")

    assert (response.status_code, response.json()) == (503, UNAVAILABLE)


def test_an_outage_during_login_or_registration_is_503(db_down):
    for path in ("/auth/login", "/auth/register"):
        response = db_down.post(path, json={"username": "alice", "password": "a perfectly fine password"})

        assert (response.status_code, response.json()) == (503, UNAVAILABLE), path
        assert "perfectly fine password" not in response.text


@pytest.fixture
def db_down(failing):
    return failing(operational_error(sqlstate=None), fail_lookup=True)


@pytest.mark.parametrize("sqlstate", ["08006", "08001", "57P01", "57P02", "57P03", "53300"])
def test_server_side_unavailability_codes_are_503(failing, sqlstate):
    response = failing(operational_error(sqlstate)).get("/tasks")

    assert (response.status_code, response.json()) == (503, UNAVAILABLE)


def test_an_invalidated_connection_is_503(failing):
    error = operational_error("40001", connection_invalidated=True)

    assert failing(error).get("/tasks").status_code == 503


@pytest.mark.parametrize("sqlstate", ["40P01", "55P03", "57014", "28P01", "42P01"])
def test_operational_errors_that_are_not_outages_stay_a_generic_500(failing, sqlstate):
    # deadlock, lock timeout, cancelled query, rejected credentials from the server, missing table
    response = failing(operational_error(sqlstate)).get("/tasks")

    assert response.status_code == 500
    assert response.text == "Internal Server Error"


@pytest.mark.parametrize(
    "error",
    [
        ProgrammingError("SELECT", {}, FakeDriverError("42P01")),
        DataError("INSERT", {}, FakeDriverError("22001")),
        IntegrityError("INSERT", {}, FakeDriverError("23514")),
        InterfaceError("SELECT", {}, FakeDriverError()),
        InvalidRequestError("session is in a bad state"),
        SATimeoutError("QueuePool limit of size 5 overflow 10 reached"),
        StaleDataError("UPDATE matched 0 rows"),
        RuntimeError("bug"),
    ],
)
def test_other_failures_stay_a_generic_500(failing, error):
    response = failing(error).get("/tasks")

    assert response.status_code == 500
    assert response.text == "Internal Server Error"
    assert SECRET_USER not in response.text


@pytest.mark.parametrize(
    "driver_error",
    [
        psycopg.OperationalError,  # connection refused, server closed the connection, ...
        psycopg.errors.ConnectionTimeout,
        psycopg.errors.ConnectionFailure,
        psycopg.errors.ConnectionDoesNotExist,
        psycopg.errors.AdminShutdown,
        psycopg.errors.CrashShutdown,
        psycopg.errors.CannotConnectNow,
        psycopg.errors.TooManyConnections,
    ],
)
def test_real_psycopg_connectivity_errors_are_503(failing, driver_error):
    response = failing(OperationalError("SELECT 1", {}, driver_error("boom"))).get("/tasks")

    assert (response.status_code, response.json()) == (503, UNAVAILABLE)


@pytest.mark.parametrize(
    "driver_error",
    [
        psycopg.errors.DeadlockDetected,
        psycopg.errors.SerializationFailure,
        psycopg.errors.LockNotAvailable,
        psycopg.errors.QueryCanceled,
        psycopg.errors.IdleSessionTimeout,
        psycopg.errors.InvalidPassword,
        psycopg.errors.InvalidAuthorizationSpecification,
        psycopg.errors.DiskFull,
        psycopg.errors.OutOfMemory,
        psycopg.errors.ObjectNotInPrerequisiteState,
    ],
)
def test_real_psycopg_errors_that_are_not_outages_stay_a_generic_500(failing, driver_error):
    response = failing(OperationalError("SELECT 1", {}, driver_error("boom"))).get("/tasks")

    assert response.status_code == 500
    assert response.text == "Internal Server Error"


def test_the_classifier_distinguishes_outages_from_other_operational_errors():
    assert is_connectivity_failure(operational_error(None))
    assert is_connectivity_failure(operational_error("57P03"))
    assert not is_connectivity_failure(operational_error("55P03"))
    assert not is_connectivity_failure(operational_error("40P01"))


def test_an_outage_is_logged_with_class_names_only(failing, caplog):
    client = failing(operational_error(None))

    with caplog.at_level(logging.WARNING, logger="app.main"):
        client.get("/tasks")

    [record] = [r for r in caplog.records if r.name == "app.main"]
    message = record.getMessage()
    assert "Database unavailable" in message
    assert "OperationalError" in message and "FakeDriverError" in message
    assert record.exc_info is None  # a traceback would carry the driver's message
    for secret in (SECRET_HOST, SECRET_USER, SECRET_PASSWORD, "FATAL"):
        assert secret not in caplog.text


def test_a_failed_commit_on_create_is_503(failing):
    session_error = operational_error(None)
    client = failing(session_error)

    response = client.post("/tasks", json={"title": "never stored"})

    assert (response.status_code, response.json()) == (503, UNAVAILABLE)


def test_an_unreachable_database_gives_503_through_the_real_driver(caplog):
    # Nothing listens on this port, so psycopg itself raises the connection error.
    engine = create_engine(
        f"postgresql+psycopg://{SECRET_USER}:secret@127.0.0.1:9/nothing", connect_args={"connect_timeout": 1}
    )
    factory = sessionmaker(bind=engine)

    def session_per_request():
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = session_per_request
    try:
        with caplog.at_level(logging.WARNING, logger="app.main"):
            response = TestClient(app, raise_server_exceptions=False, headers=bearer_headers(1)).get("/tasks")
    finally:
        app.dependency_overrides.clear()
        engine.dispose()

    assert (response.status_code, response.json()) == (503, UNAVAILABLE)
    assert "OperationalError" in caplog.text
    assert SECRET_USER not in caplog.text and "secret" not in caplog.text


def test_missing_configuration_stays_a_generic_500_not_a_503(monkeypatch, tmp_path):
    # A misconfigured deployment is a different problem from an outage, so the two stay distinct.
    for name in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB", "POSTGRES_HOST", "POSTGRES_PORT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)  # no .env here
    get_settings.cache_clear()
    from app.database import get_engine, get_session_factory

    get_engine.cache_clear()
    get_session_factory.cache_clear()
    try:
        response = TestClient(app, raise_server_exceptions=False, headers=bearer_headers(1)).get("/tasks")
    finally:
        get_settings.cache_clear()

    assert response.status_code == 500
    assert response.text == "Internal Server Error"
