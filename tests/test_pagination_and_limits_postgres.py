"""Pagination and the per-user task cap against a real PostgreSQL, including concurrent creation.

Skipped unless TEST_POSTGRES_DB names a disposable database (see tests/conftest.py).
"""

import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from conftest import reset_access_settings
from fakes import bearer_headers
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from alembic import command
from app.database import get_db
from app.main import app


def new_user(engine, username):
    with engine.begin() as connection:
        return connection.execute(
            text("INSERT INTO users (username, password_hash) VALUES (:name, 'not-a-real-hash') RETURNING id"),
            {"name": username},
        ).scalar_one()


def seed(engine, owner_id, count, prefix="t"):
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tasks (owner_id, title) SELECT :owner, :prefix || g FROM generate_series(1, :n) g"),
            {"owner": owner_id, "prefix": prefix, "n": count},
        )


def task_count(engine, owner_id):
    with engine.connect() as connection:
        return connection.execute(text("SELECT count(*) FROM tasks WHERE owner_id = :o"), {"o": owner_id}).scalar_one()


@pytest.fixture
def api(test_database):
    engine, config = test_database
    command.upgrade(config, "head")
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def session_per_request():
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = session_per_request
    alice, bob = new_user(engine, "alice"), new_user(engine, "bob")
    yield SimpleNamespace(
        engine=engine,
        alice_id=alice,
        bob_id=bob,
        alice=TestClient(app, headers=bearer_headers(alice)),
        bob=TestClient(app, headers=bearer_headers(bob)),
    )
    app.dependency_overrides.clear()


@pytest.fixture
def cap_of_5(monkeypatch):
    monkeypatch.setenv("MAX_TASKS_PER_USER", "5")
    reset_access_settings()


def in_parallel(calls, timeout=60):
    """Run every call in its own thread, all released at the same moment; returns their results in order."""
    barrier = threading.Barrier(len(calls))
    results = [None] * len(calls)

    def run(index, call):
        barrier.wait()
        try:
            results[index] = call()
        except Exception as error:  # reported by the test
            results[index] = error

    threads = [threading.Thread(target=run, args=(i, call)) for i, call in enumerate(calls)]
    [t.start() for t in threads]
    [t.join(timeout) for t in threads]
    assert not any(t.is_alive() for t in threads), "a request never finished: deadlock?"
    return results


def create_as(user_id, title="x"):
    return lambda: TestClient(app, headers=bearer_headers(user_id)).post("/tasks", json={"title": title})


# --- pagination ----------------------------------------------------------------------------------------------


def test_pages_come_from_sql_with_stable_order_and_the_right_total(api):
    seed(api.engine, api.alice_id, 120)
    seed(api.engine, api.bob_id, 30, prefix="b")
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if "FROM tasks" in statement and "count(*)" not in statement:
            statements.append(" ".join(statement.split()))

    event.listen(Engine, "before_cursor_execute", capture)
    try:
        first = api.alice.get("/tasks")
        window = api.alice.get("/tasks?limit=10&offset=115")
    finally:
        event.remove(Engine, "before_cursor_execute", capture)

    assert len(first.json()) == 50 and first.headers["X-Total-Count"] == "120"
    ids = [task["id"] for task in first.json()]
    assert ids == sorted(ids) and len(set(ids)) == 50
    assert [t["title"] for t in window.json()] == ["t116", "t117", "t118", "t119", "t120"]
    assert all("ORDER BY tasks.id" in s and "tasks.owner_id = " in s for s in statements)
    assert all("LIMIT " in s and "OFFSET " in s for s in statements)
    assert api.bob.get("/tasks?limit=200").headers["X-Total-Count"] == "30"
    assert all(t["title"].startswith("b") for t in api.bob.get("/tasks?limit=200").json())


def test_walking_every_page_yields_each_task_exactly_once(api):
    seed(api.engine, api.alice_id, 77)

    seen = []
    for offset in range(0, 80, 25):
        seen += [t["id"] for t in api.alice.get(f"/tasks?limit=25&offset={offset}").json()]

    assert len(seen) == 77 and seen == sorted(set(seen))


def test_the_maximum_limit_is_enforced_by_the_api(api):
    seed(api.engine, api.alice_id, 250)

    assert len(api.alice.get("/tasks?limit=200").json()) == 200
    assert api.alice.get("/tasks?limit=201").status_code == 422


# --- the cap -------------------------------------------------------------------------------------------------


def test_creation_stops_at_the_cap_with_409_and_stores_nothing_more(api, cap_of_5):
    codes = [api.alice.post("/tasks", json={"title": f"t{n}"}).status_code for n in range(7)]

    assert codes == [201] * 5 + [409, 409]
    assert task_count(api.engine, api.alice_id) == 5
    assert api.alice.post("/tasks", json={"title": "x"}).json() == {"detail": "Task limit of 5 reached"}


def test_a_full_user_does_not_block_another_user(api, cap_of_5):
    seed(api.engine, api.alice_id, 5)

    assert api.alice.post("/tasks", json={"title": "x"}).status_code == 409
    assert api.bob.post("/tasks", json={"title": "x"}).status_code == 201


def test_concurrent_creation_near_the_cap_never_exceeds_it(api, cap_of_5):
    seed(api.engine, api.alice_id, 3)  # two slots left

    responses = in_parallel([create_as(api.alice_id, f"c{n}") for n in range(12)])

    codes = sorted(r.status_code for r in responses)
    assert codes == [201, 201] + [409] * 10
    assert task_count(api.engine, api.alice_id) == 5


def test_concurrent_creation_from_empty_lands_exactly_on_the_cap(api, cap_of_5):
    responses = in_parallel([create_as(api.alice_id, f"c{n}") for n in range(15)])

    assert sorted(r.status_code for r in responses) == [201] * 5 + [409] * 10
    assert task_count(api.engine, api.alice_id) == 5


def test_two_users_racing_at_their_caps_each_get_exactly_their_own_share(api, cap_of_5):
    seed(api.engine, api.alice_id, 3)
    seed(api.engine, api.bob_id, 1, prefix="b")

    calls = [create_as(api.alice_id) for _ in range(6)] + [create_as(api.bob_id) for _ in range(8)]
    responses = in_parallel(calls)

    assert [r.status_code for r in responses[:6]].count(201) == 2
    assert [r.status_code for r in responses[6:]].count(201) == 4
    assert {r.status_code for r in responses} == {201, 409}
    assert (task_count(api.engine, api.alice_id), task_count(api.engine, api.bob_id)) == (5, 5)


def test_creates_deletes_and_updates_together_never_deadlock_or_fail(api, cap_of_5):
    seed(api.engine, api.alice_id, 3)
    with api.engine.connect() as connection:
        ids = [
            row[0] for row in connection.execute(text("SELECT id FROM tasks WHERE owner_id = :o"), {"o": api.alice_id})
        ]

    calls = (
        [create_as(api.alice_id) for _ in range(6)]
        + [lambda i=i: api.alice.delete(f"/tasks/{i}") for i in ids[:2]]
        + [lambda i=i: api.alice.patch(f"/tasks/{i}", json={"title": "renamed"}) for i in ids]
    )
    responses = in_parallel(calls)

    assert all(not isinstance(r, Exception) for r in responses), responses
    assert all(r.status_code in (200, 201, 204, 404, 409) for r in responses)  # never a 500 or 503
    assert task_count(api.engine, api.alice_id) <= 5


@contextmanager
def paused_while_holding_the_user_lock():
    """Pause the first request right after it locked its user's row (SELECT ... FOR NO KEY UPDATE)."""
    locked, release = threading.Event(), threading.Event()

    def pause(conn, cursor, statement, parameters, context, executemany):
        if "FOR NO KEY UPDATE" in statement and not locked.is_set():
            locked.set()
            release.wait(timeout=20)

    event.listen(Engine, "after_cursor_execute", pause)
    try:
        yield locked, release
    finally:
        release.set()
        event.remove(Engine, "after_cursor_execute", pause)


def test_a_creation_holding_one_users_lock_delays_neither_another_user_nor_task_edits(api, cap_of_5):
    seed(api.engine, api.alice_id, 1)
    with paused_while_holding_the_user_lock() as (locked, release):
        outcome = {}
        thread = threading.Thread(target=lambda: outcome.update(response=create_as(api.alice_id)()))
        thread.start()
        assert locked.wait(timeout=20), "the creation never reached its lock"

        bob = api.bob.post("/tasks", json={"title": "not delayed"})  # a different user's row
        edit = api.alice.get("/tasks")  # reading Alice's tasks is not blocked either
        waiting = threading.Thread(target=lambda: outcome.update(second=create_as(api.alice_id)()))
        waiting.start()
        waiting.join(timeout=1.5)
        still_waiting = waiting.is_alive()  # Alice's own second creation must wait its turn
        release.set()
        thread.join(timeout=20)
        waiting.join(timeout=20)

    assert bob.status_code == 201
    assert edit.status_code == 200
    assert still_waiting
    assert outcome["response"].status_code == 201 and outcome["second"].status_code == 201
    assert task_count(api.engine, api.alice_id) == 3
