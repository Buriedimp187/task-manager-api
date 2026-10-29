import logging

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.config import get_settings
from app.database import get_db, get_engine, get_session_factory
from app.main import app

client = TestClient(app)


class WorkingSession:
    def execute(self, statement):
        self.statement = str(statement)


class BrokenSession:
    def execute(self, statement):
        raise OperationalError("SELECT 1", {}, Exception('password authentication failed for user "app_user"'))


@pytest.fixture
def override_db():
    def use(session):
        app.dependency_overrides[get_db] = lambda: session

    yield use
    app.dependency_overrides.clear()


def test_health_db_returns_ok_when_the_database_answers(override_db):
    session = WorkingSession()
    override_db(session)

    response = client.get("/health/db")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert session.statement == "SELECT 1"


def test_health_db_returns_503_with_a_generic_body_when_the_database_fails(override_db):
    override_db(BrokenSession())

    response = client.get("/health/db")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
    assert "password" not in response.text
    assert "app_user" not in response.text


def test_health_db_logs_a_warning_without_secrets_when_the_database_fails(override_db, caplog):
    override_db(BrokenSession())

    with caplog.at_level(logging.WARNING, logger="app.main"):
        client.get("/health/db")

    [record] = [r for r in caplog.records if r.name == "app.main"]
    assert record.levelno == logging.WARNING
    assert "OperationalError" in record.getMessage()
    assert record.exc_info is None  # a traceback would carry the driver's message
    for leaked in ("password", "app_user", "SELECT 1"):
        assert leaked not in caplog.text


def test_health_db_does_not_log_when_the_database_answers(override_db, caplog):
    override_db(WorkingSession())

    with caplog.at_level(logging.WARNING, logger="app.main"):
        client.get("/health/db")

    assert [r for r in caplog.records if r.name == "app.main"] == []


def test_health_db_reports_missing_configuration_as_500_not_503(monkeypatch, tmp_path):
    # A missing setting is a deployment error, not a database outage, so it stays distinguishable.
    for name in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB", "POSTGRES_HOST", "POSTGRES_PORT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)  # no .env file here
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    try:
        response = TestClient(app, raise_server_exceptions=False).get("/health/db")
    finally:
        get_settings.cache_clear()

    assert response.status_code == 500
    assert response.text == "Internal Server Error"  # generic: no setting names or values


def test_liveness_does_not_depend_on_the_database(override_db):
    def fail():
        raise AssertionError("/health must not open a database session")

    app.dependency_overrides[get_db] = fail

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_openapi_documents_the_readiness_endpoint():
    operation = client.get("/openapi.json").json()["paths"]["/health/db"]["get"]

    assert {"200", "503"} <= set(operation["responses"])
