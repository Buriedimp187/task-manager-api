import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app import database


@pytest.fixture
def in_memory_sessions(monkeypatch):
    # SQLite stands in for PostgreSQL: only the session lifecycle is under test here.
    engine = create_engine("sqlite://")
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "get_session_factory", lambda: factory)
    yield
    engine.dispose()


def test_get_db_closes_the_session_when_the_request_finishes(in_memory_sessions):
    dependency = database.get_db()
    session = next(dependency)
    session.execute(text("SELECT 1"))
    assert session.in_transaction()

    with pytest.raises(StopIteration):
        next(dependency)

    assert not session.in_transaction()


def test_get_db_closes_the_session_when_the_route_raises(in_memory_sessions):
    dependency = database.get_db()
    session = next(dependency)
    session.execute(text("SELECT 1"))

    with pytest.raises(RuntimeError):
        dependency.throw(RuntimeError("simulated route failure"))

    assert not session.in_transaction()
