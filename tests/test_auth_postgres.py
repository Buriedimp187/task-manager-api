"""Registration and login against real PostgreSQL.

Skipped unless TEST_POSTGRES_DB names a disposable database (see tests/conftest.py).
"""

import logging
import threading
from types import SimpleNamespace

import jwt
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from alembic import command
from app import security
from app.config import get_auth_settings
from app.database import get_db
from app.main import app

PASSWORD = "correct horse battery staple"
BAD_LOGIN = {"detail": "Incorrect username or password"}


@pytest.fixture
def stack(test_database):
    engine, config = test_database
    command.upgrade(config, "head")
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def session_per_request():
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = session_per_request
    yield SimpleNamespace(client=TestClient(app), engine=engine)
    app.dependency_overrides.clear()


def register(client, username="alice", password=PASSWORD):
    return client.post("/auth/register", json={"username": username, "password": password})


def login(client, username="alice", password=PASSWORD):
    return client.post("/auth/login", json={"username": username, "password": password})


def users(stack):
    with stack.engine.connect() as connection:
        return [
            tuple(row) for row in connection.execute(text("SELECT id, username, password_hash FROM users ORDER BY id"))
        ]


def test_the_whole_flow_register_login_and_use_the_api(stack):
    registered = register(stack.client)
    token = login(stack.client).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    created = stack.client.post("/tasks", json={"title": "My first task"}, headers=headers)

    assert registered.status_code == 201 and created.status_code == 201
    assert jwt.decode(token, get_auth_settings().secret_key.get_secret_value(), algorithms=["HS256"])["sub"] == str(
        registered.json()["id"]
    )
    assert stack.client.get("/tasks", headers=headers).json() == [created.json()]
    with stack.engine.connect() as connection:
        owner = connection.execute(text("SELECT owner_id FROM tasks")).scalar_one()
    assert owner == registered.json()["id"]  # owned by the account that logged in


def test_only_an_argon2_hash_reaches_the_database(stack):
    register(stack.client)

    [(user_id, username, password_hash)] = users(stack)
    with stack.engine.connect() as connection:
        whole_row = connection.execute(text("SELECT users::text FROM users")).scalar_one()

    assert username == "alice" and password_hash.startswith("$argon2id$")
    assert PASSWORD not in whole_row and PASSWORD not in password_hash


def test_registration_and_login_work_with_the_production_hashing_parameters(stack, monkeypatch):
    # The other tests use a cheap hasher for speed; this one runs the real configuration once.
    monkeypatch.setattr(security, "password_hasher", PasswordHasher(**security.ARGON2_PARAMETERS))

    assert register(stack.client).status_code == 201
    assert login(stack.client).status_code == 200
    assert login(stack.client, password="the wrong password").status_code == 401
    [(_, _, password_hash)] = users(stack)
    assert "m=19456,t=2,p=1" in password_hash


@pytest.mark.parametrize("second", ["alice", "Alice", "ALICE", "aLiCe"])
def test_a_username_differing_only_in_case_cannot_be_registered(stack, second):
    first = register(stack.client, "alice")

    clash = register(stack.client, second, password="another good password")

    assert first.status_code == 201
    assert (clash.status_code, clash.json()) == (409, {"detail": "Username already taken"})
    assert [username for _, username, _ in users(stack)] == ["alice"]


def test_login_ignores_the_case_of_the_username(stack):
    register(stack.client, "Alice")

    assert login(stack.client, "alice").status_code == 200
    assert login(stack.client, "ALICE").status_code == 200


def test_a_wrong_password_and_an_unknown_user_are_indistinguishable(stack):
    register(stack.client)

    wrong = login(stack.client, "alice", "this is not the password")
    unknown = login(stack.client, "nobody", "this is not the password")

    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json() == BAD_LOGIN
    assert dict(wrong.headers) == dict(unknown.headers)


def test_two_simultaneous_registrations_of_one_name_create_one_account(stack):
    # Both requests are held until each has seen that the name is free, so both really do try to insert.
    barrier = threading.Barrier(2, timeout=20)

    def wait_after_the_availability_check(conn, cursor, statement, parameters, context, executemany):
        if "lower(users.username)" in statement:
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass

    event.listen(Engine, "after_cursor_execute", wait_after_the_availability_check)
    responses = []
    try:
        threads = [
            threading.Thread(target=lambda name=name: responses.append(register(TestClient(app), name)))
            for name in ("Mallory", "mallory")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
    finally:
        event.remove(Engine, "after_cursor_execute", wait_after_the_availability_check)

    assert sorted(response.status_code for response in responses) == [201, 409]
    assert len(users(stack)) == 1  # PostgreSQL's unique index decided, not the application


def test_a_deleted_account_can_no_longer_log_in_or_use_its_token(stack):
    register(stack.client)
    token = login(stack.client).json()["access_token"]
    with stack.engine.begin() as connection:
        connection.execute(text("DELETE FROM users"))

    assert login(stack.client).json() == BAD_LOGIN
    assert stack.client.get("/tasks", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_secrets_never_reach_the_logs_in_real_flows(stack, caplog):
    with caplog.at_level(logging.DEBUG):
        register(stack.client)
        token = login(stack.client).json()["access_token"]
        login(stack.client, password="the wrong password")
        stack.client.get("/tasks", headers={"Authorization": f"Bearer {token}"})
        stack.client.get("/tasks", headers={"Authorization": "Bearer garbage"})
        register(stack.client, "alice")  # 409

    [(_, _, password_hash)] = users(stack)
    for secret in (
        PASSWORD,
        "the wrong password",
        token,
        password_hash,
        get_auth_settings().secret_key.get_secret_value(),
    ):
        assert secret not in caplog.text


def test_authentication_requests_release_their_connections(stack):
    register(stack.client)
    for _ in range(5):
        login(stack.client)
        login(stack.client, password="wrong password here")
        register(stack.client, "ALICE")

    assert stack.engine.pool.checkedout() == 0
