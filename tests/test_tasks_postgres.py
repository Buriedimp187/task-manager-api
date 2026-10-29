"""Task endpoints against a real PostgreSQL database.

Skipped unless TEST_POSTGRES_DB names a disposable database (see tests/conftest.py). Nothing here
is evidence from a fake: persistence, ordering, locking and constraints all come from PostgreSQL.
"""

import logging
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace

import pytest
from fakes import bearer_headers
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from alembic import command
from app.database import get_db
from app.main import app
from app.models import Task

NOT_FOUND = {"detail": "Task not found"}


def new_user(engine, username):
    with engine.begin() as connection:
        return connection.execute(
            text("INSERT INTO users (username, password_hash) VALUES (:name, 'not-a-real-hash') RETURNING id"),
            {"name": username},
        ).scalar_one()


@pytest.fixture
def api(test_database):
    """Two accounts and a client for each. Every request gets its own session on the test database."""
    engine, config = test_database
    command.upgrade(config, "head")  # the schema only ever comes from the migrations
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def session_per_request():
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = session_per_request
    alice, bob = new_user(engine, "alice"), new_user(engine, "bob")
    yield SimpleNamespace(
        client=TestClient(app, headers=bearer_headers(alice)),  # Alice, unless a test says otherwise
        bob=TestClient(app, headers=bearer_headers(bob)),
        alice_id=alice,
        bob_id=bob,
        engine=engine,
        factory=factory,
    )
    app.dependency_overrides.clear()


def stored_tasks(api):
    with api.factory() as session:
        return session.query(Task).order_by(Task.id).all()


def insert_directly(api, *ids, title="seeded", owner_id=None):
    owner_id = owner_id or api.alice_id
    with api.engine.begin() as connection:
        for task_id in ids:
            connection.execute(
                text("INSERT INTO tasks (id, owner_id, title) VALUES (:id, :owner, :title)"),
                {"id": task_id, "owner": owner_id, "title": title},
            )


# --- POST /tasks ---------------------------------------------------------------------------------


def test_created_task_is_persisted_and_visible_from_another_session(api):
    response = api.client.post("/tasks", json={"title": "  Persist me  ", "description": " details "})

    assert response.status_code == 201
    body = response.json()
    [row] = stored_tasks(api)  # read through a brand-new session
    assert (row.id, row.title, row.description) == (body["id"], "Persist me", "details")
    assert row.status.value == "todo"  # the default is applied by the database
    assert row.created_at.tzinfo is not None
    assert datetime.fromisoformat(body["created_at"]) == row.created_at


def test_ids_are_assigned_by_the_database_in_increasing_order(api):
    ids = [api.client.post("/tasks", json={"title": f"task {n}"}).json()["id"] for n in range(3)]

    assert ids == sorted(ids) and len(set(ids)) == 3


def test_values_at_the_documented_limits_are_stored(api):
    title, description = "t" * 200, "d" * 2000

    response = api.client.post("/tasks", json={"title": title, "description": description})

    assert response.status_code == 201
    [row] = stored_tasks(api)
    assert (row.title, row.description) == (title, description)


def test_unicode_survives_a_round_trip(api):
    title = "café 中文 \U0001f600"

    created = api.client.post("/tasks", json={"title": title}).json()

    assert created["title"] == title
    assert api.client.get(f"/tasks/{created['id']}").json()["title"] == title


@pytest.mark.parametrize(
    "payload",
    [
        {"title": "bad\u0000title"},
        {"title": "ok", "description": "bad\u0000text"},
        {"title": "x" * 201},
        {"title": "   "},
        {"title": "ok", "status": "archived"},
    ],
)
def test_invalid_values_are_rejected_with_422_and_nothing_is_stored(api, payload):
    response = api.client.post("/tasks", json=payload)

    assert response.status_code == 422
    assert stored_tasks(api) == []


def test_a_lone_surrogate_is_rejected_with_422_and_nothing_is_stored(api):
    response = api.client.post(
        "/tasks", content='{"title": "lone \\ud800"}', headers={"content-type": "application/json"}
    )

    assert response.status_code == 422
    assert "<unencodable text>" in response.text
    assert stored_tasks(api) == []


# --- GET /tasks ----------------------------------------------------------------------------------


def test_list_is_empty_on_an_empty_table(api):
    assert api.client.get("/tasks").json() == []


def test_list_is_ordered_by_id_regardless_of_insertion_order(api):
    insert_directly(api, 9, 2, 5)  # inserted out of order on purpose

    response = api.client.get("/tasks")

    assert response.status_code == 200
    assert [task["id"] for task in response.json()] == [2, 5, 9]


def test_list_returns_every_stored_field(api):
    created = api.client.post("/tasks", json={"title": "complete", "description": "d", "status": "done"}).json()

    assert api.client.get("/tasks").json() == [created]


# --- GET /tasks/{id} -----------------------------------------------------------------------------


def test_get_returns_the_stored_task_and_404_for_others(api):
    created = api.client.post("/tasks", json={"title": "Find me"}).json()

    found = api.client.get(f"/tasks/{created['id']}")
    missing = api.client.get(f"/tasks/{created['id'] + 1}")

    assert (found.status_code, found.json()) == (200, created)
    assert (missing.status_code, missing.json()) == (404, NOT_FOUND)


def test_the_largest_valid_id_can_be_stored_and_fetched(api):
    insert_directly(api, 2_147_483_647, title="max id")

    assert api.client.get("/tasks/2147483647").json()["title"] == "max id"


def test_requests_do_not_leak_connections(api):
    for n in range(10):
        api.client.post("/tasks", json={"title": f"task {n}"})
        api.client.get("/tasks")
        api.client.get("/tasks/999999")

    assert api.engine.pool.checkedout() == 0


# --- PATCH /tasks/{id} ---------------------------------------------------------------------------


def test_patch_persists_only_the_fields_that_were_sent(api):
    created = api.client.post(
        "/tasks", json={"title": "Original", "description": "Keep me", "status": "in_progress"}
    ).json()

    response = api.client.patch(f"/tasks/{created['id']}", json={"title": "  Renamed  "})

    assert response.status_code == 200
    [row] = stored_tasks(api)
    assert (row.title, row.description, row.status.value) == ("Renamed", "Keep me", "in_progress")
    assert row.created_at == datetime.fromisoformat(created["created_at"])  # never touched


def test_patch_with_null_description_stores_sql_null(api):
    created = api.client.post("/tasks", json={"title": "t", "description": "to clear"}).json()

    response = api.client.patch(f"/tasks/{created['id']}", json={"description": None})

    assert response.status_code == 200 and response.json()["description"] is None
    with api.engine.connect() as connection:
        assert connection.execute(text("SELECT description IS NULL FROM tasks")).scalar_one() is True


def test_patch_can_move_a_task_through_every_status(api):
    created = api.client.post("/tasks", json={"title": "t"}).json()

    for status in ("in_progress", "done", "todo"):
        assert api.client.patch(f"/tasks/{created['id']}", json={"status": status}).json()["status"] == status

    assert stored_tasks(api)[0].status.value == "todo"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"title": None},
        {"status": None},
        {"title": "   "},
        {"title": "bad\u0000"},
        {"status": "archived"},
        {"id": 9},
    ],
)
def test_rejected_patches_leave_the_stored_row_unchanged(api, payload):
    created = api.client.post("/tasks", json={"title": "Stable", "description": "d"}).json()

    response = api.client.patch(f"/tasks/{created['id']}", json=payload)

    assert response.status_code == 422
    [row] = stored_tasks(api)
    assert (row.title, row.description, row.status.value) == ("Stable", "d", "todo")


def test_patch_returns_404_for_a_missing_task(api):
    response = api.client.patch("/tasks/12345", json={"title": "x"})

    assert (response.status_code, response.json()) == (404, NOT_FOUND)
    assert stored_tasks(api) == []


# --- DELETE /tasks/{id} --------------------------------------------------------------------------


def test_delete_removes_exactly_that_row(api):
    first = api.client.post("/tasks", json={"title": "one"}).json()
    second = api.client.post("/tasks", json={"title": "two"}).json()

    response = api.client.delete(f"/tasks/{first['id']}")

    assert response.status_code == 204 and response.content == b""
    assert [row.id for row in stored_tasks(api)] == [second["id"]]
    assert api.client.delete(f"/tasks/{first['id']}").status_code == 404


def test_delete_returns_404_for_a_missing_task(api):
    response = api.client.delete("/tasks/12345")

    assert (response.status_code, response.json()) == (404, NOT_FOUND)


# --- Failures leave nothing half-done -------------------------------------------------------------

UNAVAILABLE = {"detail": "Database unavailable"}


class ConnectionLost(Exception):
    sqlstate = None  # like a driver error raised before the server could answer


def commit_that_fails(session):
    session.flush()  # the change reaches PostgreSQL inside the still-open transaction...
    raise OperationalError("COMMIT", {}, ConnectionLost("server closed the connection"))  # ...never committed


def test_a_failed_commit_on_create_stores_nothing(api, monkeypatch):
    with monkeypatch.context() as patched:
        patched.setattr(Session, "commit", commit_that_fails)
        response = api.client.post("/tasks", json={"title": "never stored"})

    assert (response.status_code, response.json()) == (503, UNAVAILABLE)
    assert stored_tasks(api) == []
    assert api.engine.pool.checkedout() == 0


def test_a_failed_commit_on_patch_changes_nothing_and_releases_the_row_lock(api, monkeypatch):
    insert_directly(api, 1, title="seed")

    with monkeypatch.context() as patched:
        patched.setattr(Session, "commit", commit_that_fails)
        failed = api.client.patch("/tasks/1", json={"title": "half done"})

    assert (failed.status_code, failed.json()) == (503, UNAVAILABLE)
    assert [row.title for row in stored_tasks(api)] == ["seed"]
    started = time.monotonic()
    assert api.client.patch("/tasks/1", json={"title": "retried"}).status_code == 200
    assert time.monotonic() - started < 5  # the failed request did not keep the row locked
    assert api.engine.pool.checkedout() == 0


def test_a_failed_commit_on_delete_keeps_the_row_and_releases_the_lock(api, monkeypatch):
    insert_directly(api, 1, title="seed")

    with monkeypatch.context() as patched:
        patched.setattr(Session, "commit", commit_that_fails)
        failed = api.client.delete("/tasks/1")

    assert (failed.status_code, failed.json()) == (503, UNAVAILABLE)
    assert [row.title for row in stored_tasks(api)] == ["seed"]
    assert api.client.delete("/tasks/1").status_code == 204


def test_a_lock_timeout_is_a_retryable_503(api):
    # PostgreSQL reports it as an OperationalError (SQLSTATE 55P03): the database is up but busy.
    insert_directly(api, 1, title="held")

    def session_with_a_short_lock_timeout():
        with api.factory() as session:
            session.execute(text("SET lock_timeout = '500ms'"))
            yield session

    holder = api.engine.connect()
    try:
        holder.execute(text("SELECT id FROM tasks WHERE id = 1 FOR UPDATE"))  # another session holds the row
        app.dependency_overrides[get_db] = session_with_a_short_lock_timeout
        response = TestClient(app, raise_server_exceptions=False, headers=api.client.headers).patch(
            "/tasks/1", json={"title": "x"}
        )
    finally:
        holder.rollback()
        holder.close()

    assert (response.status_code, response.json()) == (503, {"detail": "Database busy, retry shortly"})
    assert response.headers["Retry-After"] == "1"
    assert [row.title for row in stored_tasks(api)] == ["held"]


def test_a_connection_killed_by_the_server_is_503_and_the_next_request_recovers(api):
    assert api.client.get("/tasks").status_code == 200  # leaves a live connection in the pool
    admin = create_engine(api.engine.url, poolclass=NullPool, connect_args={"connect_timeout": 5})
    with admin.begin() as connection:
        connection.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = current_database() AND pid <> pg_backend_pid()"
            )
        )
    admin.dispose()

    killed = api.client.get("/tasks")
    recovered = api.client.get("/tasks")

    assert (killed.status_code, killed.json()) == (503, UNAVAILABLE)
    assert recovered.status_code == 200
    assert api.engine.pool.checkedout() == 0


def test_wrong_credentials_surface_as_a_generic_503_without_leaking_them(api, caplog):
    # A connect-time authentication failure carries no SQLSTATE, so it is reported like any outage.
    wrong_password = "definitely-wrong-password"
    bad_engine = create_engine(api.engine.url.set(password=wrong_password), connect_args={"connect_timeout": 3})
    factory = sessionmaker(bind=bad_engine)

    def session_per_request():
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = session_per_request
    try:
        with caplog.at_level(logging.WARNING, logger="app.main"):
            response = api.client.get("/tasks")
    finally:
        bad_engine.dispose()

    assert (response.status_code, response.json()) == (503, UNAVAILABLE)
    assert wrong_password not in response.text and wrong_password not in caplog.text
    assert api.engine.url.username not in response.text and api.engine.url.username not in caplog.text


# --- Concurrency: PATCH and DELETE take a row lock ------------------------------------------------


@contextmanager
def paused_after_the_row_lock():
    """Pause the first request that runs SELECT ... FOR UPDATE, right after it takes the row lock.

    Only one request is in flight when this is armed, and later ones are not paused. (Sync handlers
    run in worker threads, so a thread name cannot identify the request.)
    """
    locked, release = threading.Event(), threading.Event()

    def pause(conn, cursor, statement, parameters, context, executemany):
        if "FOR UPDATE" in statement and not locked.is_set():
            locked.set()
            release.wait(timeout=20)

    event.listen(Engine, "after_cursor_execute", pause)
    try:
        yield locked, release
    finally:
        release.set()
        event.remove(Engine, "after_cursor_execute", pause)


def in_thread(api, name, call):
    """Run `call` (which issues one request) in a named thread; returns the thread and its outcome."""
    outcome = {}

    def target():
        try:
            outcome["response"] = call(TestClient(app, headers=api.client.headers))
        except Exception as error:  # reported by the test, from the main thread
            outcome["error"] = error

    thread = threading.Thread(target=target, name=name)
    thread.start()
    return thread, outcome


def test_a_delete_waits_for_a_patch_that_holds_the_row_and_neither_fails(api):
    task_id = api.client.post("/tasks", json={"title": "contended"}).json()["id"]

    with paused_after_the_row_lock() as (locked, release):
        patcher, patch = in_thread(
            api, "patch-thread", lambda c: c.patch(f"/tasks/{task_id}", json={"title": "patched"})
        )
        assert locked.wait(timeout=20), "the PATCH never reached its row lock"
        deleter, delete = in_thread(api, "delete-thread", lambda c: c.delete(f"/tasks/{task_id}"))
        time.sleep(1)
        assert deleter.is_alive(), "the DELETE should be waiting for the row lock"
        release.set()
        patcher.join(timeout=30)
        deleter.join(timeout=30)

    assert "error" not in patch and "error" not in delete
    assert patch["response"].status_code == 200 and patch["response"].json()["title"] == "patched"
    assert delete["response"].status_code == 204
    assert stored_tasks(api) == []


def test_a_patch_that_loses_the_race_to_a_delete_gets_404_not_500(api):
    task_id = api.client.post("/tasks", json={"title": "contended"}).json()["id"]

    with paused_after_the_row_lock() as (locked, release):
        deleter, delete = in_thread(api, "delete-thread", lambda c: c.delete(f"/tasks/{task_id}"))
        assert locked.wait(timeout=20), "the DELETE never reached its row lock"
        patcher, patch = in_thread(api, "patch-thread", lambda c: c.patch(f"/tasks/{task_id}", json={"title": "late"}))
        time.sleep(1)
        assert patcher.is_alive(), "the PATCH should be waiting for the row lock"
        release.set()
        deleter.join(timeout=30)
        patcher.join(timeout=30)

    assert "error" not in patch and "error" not in delete
    assert delete["response"].status_code == 204
    assert (patch["response"].status_code, patch["response"].json()) == (404, NOT_FOUND)
    assert stored_tasks(api) == []


def test_reads_are_not_blocked_by_a_patch_in_progress(api):
    task_id = api.client.post("/tasks", json={"title": "before"}).json()["id"]

    with paused_after_the_row_lock() as (locked, release):
        patcher, patch = in_thread(api, "patch-thread", lambda c: c.patch(f"/tasks/{task_id}", json={"title": "after"}))
        assert locked.wait(timeout=20)
        started = time.monotonic()
        during = api.client.get(f"/tasks/{task_id}")
        elapsed = time.monotonic() - started
        release.set()
        patcher.join(timeout=30)

    assert during.status_code == 200 and during.json()["title"] == "before"  # the uncommitted change is invisible
    assert elapsed < 5
    assert patch["response"].status_code == 200
    assert api.client.get(f"/tasks/{task_id}").json()["title"] == "after"


# --- Ownership: the same guarantees against real PostgreSQL -----------------------------------------


def snapshot(api):
    """Every task row, completely, so 'nothing changed' can be asserted exactly."""
    with api.engine.connect() as connection:
        rows = connection.execute(
            text("SELECT id, owner_id, title, description, status, created_at FROM tasks ORDER BY id")
        )
        return [tuple(row) for row in rows]


def test_the_owner_is_stored_from_the_token(api):
    api.client.post("/tasks", json={"title": "alice's"})
    api.bob.post("/tasks", json={"title": "bob's"})

    assert {task.title: task.owner_id for task in stored_tasks(api)} == {
        "alice's": api.alice_id,
        "bob's": api.bob_id,
    }


def test_each_user_lists_only_their_own_tasks(api):
    for n in range(3):
        api.client.post("/tasks", json={"title": f"alice {n}"})
        api.bob.post("/tasks", json={"title": f"bob {n}"})

    assert [t["title"] for t in api.client.get("/tasks").json()] == ["alice 0", "alice 1", "alice 2"]
    assert [t["title"] for t in api.bob.get("/tasks").json()] == ["bob 0", "bob 1", "bob 2"]


def test_another_users_task_cannot_be_read_changed_or_deleted(api):
    alices = api.client.post("/tasks", json={"title": "private", "description": "secret"}).json()
    before = snapshot(api)

    responses = [
        api.bob.get(f"/tasks/{alices['id']}"),
        api.bob.patch(f"/tasks/{alices['id']}", json={"title": "hijacked", "status": "done", "description": None}),
        api.bob.delete(f"/tasks/{alices['id']}"),
    ]

    assert [(r.status_code, r.json()) for r in responses] == [(404, NOT_FOUND)] * 3
    assert snapshot(api) == before  # not a single column of any row changed
    assert api.client.get(f"/tasks/{alices['id']}").json() == alices


def test_a_foreign_task_looks_exactly_like_a_missing_one(api):
    alices = api.client.post("/tasks", json={"title": "private"}).json()
    missing = alices["id"] + 1000

    for method, kwargs in [("get", {}), ("patch", {"json": {"title": "x"}}), ("delete", {})]:
        foreign = getattr(api.bob, method)(f"/tasks/{alices['id']}", **kwargs)
        absent = getattr(api.bob, method)(f"/tasks/{missing}", **kwargs)

        assert foreign.status_code == absent.status_code == 404, method
        assert foreign.content == absent.content, method
        assert dict(foreign.headers) == dict(absent.headers), method


def test_guessing_ids_never_reveals_anothers_tasks(api):
    created = [api.client.post("/tasks", json={"title": f"alice {n}"}).json()["id"] for n in range(5)]

    results = [api.bob.get(f"/tasks/{task_id}").status_code for task_id in range(1, max(created) + 3)]

    assert set(results) == {404}
    assert api.bob.get("/tasks").json() == []


@pytest.mark.parametrize("field", ["owner_id", "user_id", "owner", "ownerId"])
def test_forged_ownership_is_rejected_and_changes_nothing(api, field):
    alices = api.client.post("/tasks", json={"title": "mine"}).json()
    before = snapshot(api)

    create = api.bob.post("/tasks", json={"title": "forged", field: api.alice_id})
    update = api.bob.patch(f"/tasks/{alices['id']}", json={field: api.bob_id})
    steal = api.client.patch(f"/tasks/{alices['id']}", json={field: api.bob_id})

    # Rejected by validation, before any lookup, so the answer does not depend on who owns what.
    assert (create.status_code, update.status_code, steal.status_code) == (422, 422, 422)
    assert snapshot(api) == before


def test_a_forged_query_string_or_header_cannot_choose_the_owner(api):
    response = api.bob.post(
        f"/tasks?owner_id={api.alice_id}",
        json={"title": "bob's"},
        headers={"X-Owner-Id": str(api.alice_id), "X-User-Id": str(api.alice_id)},
    )

    assert response.status_code == 201
    [task] = stored_tasks(api)
    assert task.owner_id == api.bob_id


def test_a_request_for_a_foreign_task_does_not_wait_for_the_owners_row_lock(api):
    # The owner test is part of the locking SELECT, so a task that is not yours is never even looked at
    # under lock: no waiting, and nothing to learn from the timing.
    task_id = api.client.post("/tasks", json={"title": "contended"}).json()["id"]

    with paused_after_the_row_lock() as (locked, release):
        patcher, patch = in_thread(api, "alice-patch", lambda c: c.patch(f"/tasks/{task_id}", json={"title": "new"}))
        assert locked.wait(timeout=20), "Alice's PATCH never reached its row lock"
        started = time.monotonic()
        foreign = api.bob.patch(f"/tasks/{task_id}", json={"title": "hijack"})
        foreign_delete = api.bob.delete(f"/tasks/{task_id}")
        elapsed = time.monotonic() - started
        release.set()
        patcher.join(timeout=30)

    assert (foreign.status_code, foreign_delete.status_code) == (404, 404)
    assert elapsed < 5  # it did not queue behind Alice's lock
    assert patch["response"].status_code == 200
    assert api.client.get(f"/tasks/{task_id}").json()["title"] == "new"


def test_different_users_never_block_each_other(api):
    mine = api.client.post("/tasks", json={"title": "mine"}).json()["id"]
    theirs = api.bob.post("/tasks", json={"title": "theirs"}).json()["id"]

    with paused_after_the_row_lock() as (locked, release):
        patcher, patch = in_thread(api, "alice-patch", lambda c: c.patch(f"/tasks/{mine}", json={"title": "edited"}))
        assert locked.wait(timeout=20)
        started = time.monotonic()
        bobs = api.bob.patch(f"/tasks/{theirs}", json={"title": "edited too"})
        elapsed = time.monotonic() - started
        release.set()
        patcher.join(timeout=30)

    assert bobs.status_code == 200 and elapsed < 5  # row locks, not table locks
    assert patch["response"].status_code == 200


def test_concurrent_attempts_on_one_task_by_both_users_leave_it_with_its_owner(api):
    task_id = api.client.post("/tasks", json={"title": "contended"}).json()["id"]
    outcomes = []

    def attack(client):
        for _ in range(10):
            outcomes.append(client.patch(f"/tasks/{task_id}", json={"title": "bob was here"}).status_code)
            outcomes.append(client.delete(f"/tasks/{task_id}").status_code)

    workers = [threading.Thread(target=attack, args=(TestClient(app, headers=api.bob.headers),)) for _ in range(4)]
    editor = threading.Thread(
        target=lambda: [api.client.patch(f"/tasks/{task_id}", json={"status": "in_progress"}) for _ in range(10)]
    )
    for thread in [*workers, editor]:
        thread.start()
    for thread in [*workers, editor]:
        thread.join(timeout=60)

    assert set(outcomes) == {404}  # every one of Bob's 80 attempts was refused
    [task] = stored_tasks(api)
    assert task.owner_id == api.alice_id and task.title == "contended"


def test_a_token_stops_working_when_its_account_is_deleted(api):
    api.bob.post("/tasks", json={"title": "bob's"})
    with api.engine.begin() as connection:
        connection.execute(text("DELETE FROM users WHERE id = :id"), {"id": api.bob_id})

    assert api.bob.get("/tasks").status_code == 401
    assert api.client.get("/tasks").status_code == 200  # Alice is unaffected
    assert [task.owner_id for task in stored_tasks(api)] == []  # Bob's tasks went with the account


def test_rejected_requests_release_their_connections(api):
    anonymous = TestClient(app)
    for _ in range(5):
        anonymous.get("/tasks")  # 401
        api.bob.get("/tasks/9999")  # 404
        api.client.post("/tasks", json={})  # 422
        TestClient(app, headers={"Authorization": "Bearer nonsense"}).get("/tasks")  # 401

    assert api.engine.pool.checkedout() == 0
