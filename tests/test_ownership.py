"""Who may see and change which task. These run against the in-memory fake, which evaluates the very
WHERE clauses the handlers build: if a handler stops filtering by owner, the outcome changes and the
test fails. The same scenarios run against PostgreSQL in test_tasks_postgres.py."""

import pytest
from fakes import FakeSession, bearer_headers
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models import Task, User

NOT_FOUND = {"detail": "Task not found"}
NOT_AUTHENTICATED = {"detail": "Not authenticated"}


@pytest.fixture
def db():
    session = FakeSession()
    app.dependency_overrides[get_db] = lambda: session
    yield session
    app.dependency_overrides.clear()


@pytest.fixture
def alice(db):
    return db.add_user("alice")


@pytest.fixture
def bob(db):
    return db.add_user("bob")


def as_user(user):
    return TestClient(app, headers=bearer_headers(user.id))


@pytest.fixture
def alice_client(alice):
    return as_user(alice)


@pytest.fixture
def bob_client(bob):
    return as_user(bob)


@pytest.fixture
def alices_task(alice_client):
    response = alice_client.post("/tasks", json={"title": "Alice's private task", "description": "secret"})
    assert response.status_code == 201
    return response.json()


# --- Isolation ------------------------------------------------------------------------------------


def test_each_user_lists_only_their_own_tasks(alice_client, bob_client):
    alice_client.post("/tasks", json={"title": "alice 1"})
    bob_client.post("/tasks", json={"title": "bob 1"})
    alice_client.post("/tasks", json={"title": "alice 2"})
    bob_client.post("/tasks", json={"title": "bob 2"})

    assert [task["title"] for task in alice_client.get("/tasks").json()] == ["alice 1", "alice 2"]
    assert [task["title"] for task in bob_client.get("/tasks").json()] == ["bob 1", "bob 2"]


def test_a_new_user_starts_with_no_tasks(alices_task, bob_client):
    assert bob_client.get("/tasks").json() == []


def test_another_users_task_is_a_404_to_read(alices_task, bob_client):
    response = bob_client.get(f"/tasks/{alices_task['id']}")

    assert (response.status_code, response.json()) == (404, NOT_FOUND)


def test_another_users_task_cannot_be_updated(alices_task, alice_client, bob_client):
    response = bob_client.patch(f"/tasks/{alices_task['id']}", json={"title": "hijacked", "status": "done"})

    assert (response.status_code, response.json()) == (404, NOT_FOUND)
    assert alice_client.get(f"/tasks/{alices_task['id']}").json() == alices_task  # untouched


def test_another_users_task_cannot_be_deleted(alices_task, alice_client, bob_client):
    response = bob_client.delete(f"/tasks/{alices_task['id']}")

    assert (response.status_code, response.json()) == (404, NOT_FOUND)
    assert alice_client.get(f"/tasks/{alices_task['id']}").json() == alices_task  # still there


def test_a_foreign_task_is_indistinguishable_from_a_missing_one(alices_task, bob_client):
    missing_id = alices_task["id"] + 1000
    for method, kwargs in [("GET", {}), ("PATCH", {"json": {"title": "x"}}), ("DELETE", {})]:
        foreign = bob_client.request(method, f"/tasks/{alices_task['id']}", **kwargs)
        missing = bob_client.request(method, f"/tasks/{missing_id}", **kwargs)

        assert foreign.status_code == missing.status_code == 404, method
        assert foreign.content == missing.content, method
        assert dict(foreign.headers) == dict(missing.headers), method


def test_the_attacker_can_still_use_their_own_tasks_normally(alices_task, bob_client):
    bobs = bob_client.post("/tasks", json={"title": "bob's"}).json()

    assert bob_client.patch(f"/tasks/{bobs['id']}", json={"status": "done"}).json()["status"] == "done"
    assert bob_client.delete(f"/tasks/{bobs['id']}").status_code == 204


def test_deleting_my_task_does_not_touch_anyone_elses(alice_client, bob_client):
    mine = alice_client.post("/tasks", json={"title": "mine"}).json()
    theirs = bob_client.post("/tasks", json={"title": "theirs"}).json()

    alice_client.delete(f"/tasks/{mine['id']}")

    assert bob_client.get("/tasks").json() == [theirs]


def test_list_ignores_attempts_to_choose_whose_tasks_to_see(alices_task, bob_client):
    attempts = [
        bob_client.get("/tasks", params={"owner_id": 1, "user_id": 1, "owner": "alice"}),
        bob_client.get("/tasks", headers={"X-Owner-Id": "1", "X-User-Id": "1"}),
    ]

    assert [response.json() for response in attempts] == [[], []]


# --- Ownership cannot be forged -------------------------------------------------------------------


@pytest.mark.parametrize("field", ["owner_id", "owner", "user_id", "ownerId", "user", "id"])
def test_create_rejects_an_ownership_field_in_the_body(alice, bob_client, db, field):
    response = bob_client.post("/tasks", json={"title": "forged", field: alice.id})

    assert response.status_code == 422
    assert db.rows[Task] == {}  # nothing was stored for anyone
    assert bob_client.get("/tasks").json() == []


@pytest.mark.parametrize("field", ["owner_id", "owner", "user_id", "ownerId", "user"])
def test_update_rejects_an_ownership_field_in_the_body(alices_task, alice_client, bob, field):
    response = alice_client.patch(f"/tasks/{alices_task['id']}", json={field: bob.id})

    assert response.status_code == 422
    assert alice_client.get(f"/tasks/{alices_task['id']}").json() == alices_task


def test_the_owner_is_taken_from_the_token_whatever_else_is_sent(alice, bob, bob_client, db):
    response = bob_client.post(
        "/tasks?owner_id=1",
        json={"title": "mine"},
        headers={"X-Owner-Id": str(alice.id), "X-User-Id": str(alice.id)},
    )

    assert response.status_code == 201
    [task] = db.rows[Task].values()
    assert task.owner_id == bob.id


def test_ownership_never_appears_in_a_response(alice_client, alices_task):
    assert set(alices_task) == {"id", "title", "description", "status", "created_at"}
    assert all("owner" not in key for key in alice_client.get("/tasks").json()[0])


def test_updating_a_task_never_changes_its_owner(alice, alice_client, alices_task, db):
    alice_client.patch(f"/tasks/{alices_task['id']}", json={"title": "renamed", "status": "done"})

    [task] = db.rows[Task].values()
    assert task.owner_id == alice.id


# --- Every query is owner-scoped (a complement to the behaviour above, not a substitute) --------


@pytest.mark.parametrize(
    "method, path, kwargs",
    [
        ("GET", "/tasks", {}),
        ("GET", "/tasks/1", {}),
        ("PATCH", "/tasks/1", {"json": {"title": "x"}}),
        ("DELETE", "/tasks/1", {}),
    ],
)
def test_every_task_query_filters_by_owner(alice_client, db, method, path, kwargs):
    alice_client.request(method, path, **kwargs)

    assert "tasks.owner_id = " in db.statement


# --- Authentication is required everywhere under /tasks -----------------------------------------


def task_routes():
    # Read from the published API description, so a route added later is covered automatically.
    for path, methods in app.openapi()["paths"].items():
        if path.startswith("/tasks"):
            for method in methods:
                yield method.upper(), path.replace("{task_id}", "1")


def test_there_are_five_task_operations_and_all_of_them_are_checked():
    assert sorted(task_routes()) == sorted(
        [("POST", "/tasks"), ("GET", "/tasks"), ("GET", "/tasks/1"), ("PATCH", "/tasks/1"), ("DELETE", "/tasks/1")]
    )


@pytest.mark.parametrize("method, path", sorted(task_routes()))
def test_every_task_route_requires_authentication(db, method, path):
    response = TestClient(app).request(method, path, json={"title": "x"})

    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED
    assert response.headers["www-authenticate"] == "Bearer"
    assert db.calls == []  # nothing was read or written


def test_authentication_comes_before_validation_and_lookup(db):
    anonymous = TestClient(app)

    assert anonymous.post("/tasks", json={}).status_code == 401  # would be 422 if validated first
    assert anonymous.get("/tasks/999").status_code == 401  # would be 404 if looked up first
    assert anonymous.get("/tasks/not-a-number").status_code == 401


def test_a_token_for_a_user_that_no_longer_exists_is_rejected(alice, alice_client, db):
    del db.rows[User][alice.id]

    response = alice_client.get("/tasks")

    assert (response.status_code, response.json()) == (401, NOT_AUTHENTICATED)


def test_health_endpoints_stay_public(db):
    anonymous = TestClient(app)

    assert anonymous.get("/health").json() == {"status": "ok"}
    assert anonymous.get("/health/db").json() == {"status": "ok"}
