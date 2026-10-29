"""Pagination of GET /tasks and the per-user task cap, against the in-memory fake.

The fake evaluates the WHERE clauses, LIMIT and OFFSET the handlers really build. Concurrency is covered
against PostgreSQL in test_pagination_and_limits_postgres.py.
"""

import pytest
from conftest import reset_access_settings
from fakes import FakeSession, bearer_headers
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models import Task


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
def client(alice):
    return as_user(alice)


def seed(db, owner, count, prefix="t"):
    for number in range(1, count + 1):
        task = Task(title=f"{prefix}{number}", owner_id=owner.id)
        db.add(task)
    db.commit()


def titles(response):
    return [task["title"] for task in response.json()]


# --- pagination -----------------------------------------------------------------------------------------------


def test_the_default_page_is_50_tasks_and_the_total_is_reported(client, db, alice):
    seed(db, alice, 120)

    response = client.get("/tasks")

    assert response.status_code == 200
    assert len(response.json()) == 50
    assert titles(response)[:2] == ["t1", "t2"]
    assert response.headers["X-Total-Count"] == "120"


def test_the_response_is_still_a_bare_list_of_tasks(client, db, alice):
    seed(db, alice, 1)

    body = client.get("/tasks").json()

    assert isinstance(body, list) and set(body[0]) == {"id", "title", "description", "status", "created_at"}


def test_an_empty_list_has_a_zero_total(client):
    response = client.get("/tasks")

    assert (response.json(), response.headers["X-Total-Count"]) == ([], "0")


def test_limit_and_offset_select_a_window(client, db, alice):
    seed(db, alice, 10)

    assert titles(client.get("/tasks?limit=3")) == ["t1", "t2", "t3"]
    assert titles(client.get("/tasks?limit=3&offset=3")) == ["t4", "t5", "t6"]
    assert titles(client.get("/tasks?limit=3&offset=9")) == ["t10"]
    assert client.get("/tasks?limit=3&offset=10").json() == []
    assert client.get("/tasks?offset=1000").json() == []


def test_the_total_ignores_the_window(client, db, alice):
    seed(db, alice, 10)

    for query in ("limit=1", "offset=4&limit=2", "offset=500"):
        assert client.get(f"/tasks?{query}").headers["X-Total-Count"] == "10"


def test_walking_the_pages_returns_every_task_once_in_order(client, db, alice):
    seed(db, alice, 23)

    seen = []
    for offset in range(0, 23, 5):
        seen += titles(client.get(f"/tasks?limit=5&offset={offset}"))

    assert seen == [f"t{n}" for n in range(1, 24)]


def test_the_maximum_page_size_is_200(client, db, alice):
    seed(db, alice, 250)

    assert len(client.get("/tasks?limit=200").json()) == 200
    assert client.get("/tasks?limit=201").status_code == 422


@pytest.mark.parametrize(
    "query",
    ["limit=0", "limit=-1", "limit=abc", "limit=1.5", "limit=", "offset=-1", "offset=abc", "offset=2147483648"],
)
def test_invalid_pagination_parameters_are_rejected(client, query):
    assert client.get(f"/tasks?{query}").status_code == 422


def test_pagination_is_applied_in_sql_not_after_fetching_everything(client, db, alice):
    seed(db, alice, 10)

    client.get("/tasks?limit=3&offset=2")

    assert "LIMIT" in db.statement and "OFFSET" in db.statement


def test_users_only_page_through_their_own_tasks(db, alice, bob):
    seed(db, alice, 5, prefix="a")
    seed(db, bob, 3, prefix="b")

    page = as_user(bob).get("/tasks?limit=2&offset=1")

    assert titles(page) == ["b2", "b3"]
    assert page.headers["X-Total-Count"] == "3"  # Alice's tasks are not even counted
    assert as_user(alice).get("/tasks?limit=2&offset=4").json()[0]["title"] == "a5"


def test_the_listing_still_requires_authentication(client):
    response = TestClient(app).get("/tasks?limit=1")

    assert response.status_code == 401
    assert "X-Total-Count" not in response.headers


# --- the per-user cap ----------------------------------------------------------------------------------------


@pytest.fixture
def small_cap(monkeypatch):
    monkeypatch.setenv("MAX_TASKS_PER_USER", "3")
    reset_access_settings()


def test_the_default_cap_is_1000(monkeypatch):
    from app.config import AccessSettings

    monkeypatch.delenv("MAX_TASKS_PER_USER", raising=False)

    assert AccessSettings(_env_file=None).max_tasks_per_user == 1000


@pytest.mark.parametrize("value", ["0", "-1", "many", "1.5", "1000001"])
def test_an_invalid_cap_is_rejected(monkeypatch, value):
    from pydantic import ValidationError

    from app.config import AccessSettings

    monkeypatch.setenv("MAX_TASKS_PER_USER", value)

    with pytest.raises(ValidationError):
        AccessSettings(_env_file=None)


def test_tasks_can_be_created_up_to_the_cap_and_then_get_409(client, small_cap):
    codes = [client.post("/tasks", json={"title": f"t{n}"}).status_code for n in range(5)]

    assert codes == [201, 201, 201, 409, 409]


def test_the_error_body_is_clear_and_consistent(client, small_cap, db):
    for n in range(3):
        client.post("/tasks", json={"title": f"t{n}"})
    stored = len(db.rows[Task])

    response = client.post("/tasks", json={"title": "one too many"})

    assert (response.status_code, response.json()) == (409, {"detail": "Task limit of 3 reached"})
    assert len(db.rows[Task]) == stored  # nothing was stored


def test_deleting_a_task_frees_a_slot(client, small_cap):
    ids = [client.post("/tasks", json={"title": f"t{n}"}).json()["id"] for n in range(3)]
    assert client.post("/tasks", json={"title": "x"}).status_code == 409

    assert client.delete(f"/tasks/{ids[0]}").status_code == 204

    assert client.post("/tasks", json={"title": "x"}).status_code == 201


def test_one_users_cap_does_not_affect_another(db, alice, bob, small_cap):
    seed(db, alice, 3)
    seed(db, bob, 2, prefix="b")

    assert as_user(alice).post("/tasks", json={"title": "x"}).status_code == 409
    assert as_user(bob).post("/tasks", json={"title": "x"}).status_code == 201
    assert as_user(bob).post("/tasks", json={"title": "y"}).status_code == 409


def test_only_creation_is_capped_not_reading_or_changing(client, db, alice, small_cap):
    seed(db, alice, 3)

    assert client.get("/tasks").status_code == 200
    assert client.patch("/tasks/1", json={"title": "renamed"}).status_code == 200
    assert client.delete("/tasks/2").status_code == 204


def test_a_validation_error_is_reported_before_the_cap(client, db, alice, small_cap):
    seed(db, alice, 3)

    assert client.post("/tasks", json={"title": ""}).status_code == 422


def test_creation_locks_the_users_own_row_before_counting(client, db):
    client.post("/tasks", json={"title": "x"})

    assert [c for c in db.calls if not c.startswith("get:")][:3] == [
        "scalar:User:for_update=True",
        "count:Task",
        "add:Task",
    ]


def test_an_account_that_vanishes_before_the_lock_is_not_authenticated(alice):
    # The token was valid when it was checked, but the user's row is gone by the time the creation locks it.
    class Vanishing(FakeSession):
        def get(self, model, key):
            row = super().get(model, key)
            self.rows[model].pop(key, None)
            return row

    session = Vanishing()
    user = session.add_user("alice")
    app.dependency_overrides[get_db] = lambda: session

    response = TestClient(app, headers=bearer_headers(user.id)).post("/tasks", json={"title": "x"})

    assert (response.status_code, response.json()) == (401, {"detail": "Not authenticated"})
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert session.rows[Task] == {}
