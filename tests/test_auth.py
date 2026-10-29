"""Registration, login, password hashing and access-token validation, against the in-memory fake."""

import base64
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest
from argon2 import PasswordHasher
from fakes import FakeSession, bearer_headers
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app import security
from app.config import get_auth_settings
from app.database import get_db
from app.main import app
from app.models import User

PASSWORD = "correct horse battery staple"
NOT_AUTHENTICATED = {"detail": "Not authenticated"}
BAD_LOGIN = {"detail": "Incorrect username or password"}


@pytest.fixture
def db():
    session = FakeSession()
    app.dependency_overrides[get_db] = lambda: session
    yield session
    app.dependency_overrides.clear()


@pytest.fixture
def client(db):
    return TestClient(app)


@pytest.fixture
def lenient(db):
    return TestClient(app, raise_server_exceptions=False)


def register(client, username="alice", password=PASSWORD):
    return client.post("/auth/register", json={"username": username, "password": password})


def login(client, username="alice", password=PASSWORD):
    return client.post("/auth/login", json={"username": username, "password": password})


def secret():
    return get_auth_settings().secret_key.get_secret_value()


def token(claims=None, key=None, algorithm="HS256"):
    now = datetime.now(UTC)
    payload = {"sub": "1", "iat": now, "exp": now + timedelta(minutes=5)}
    payload.update(claims or {})
    payload = {name: value for name, value in payload.items() if value is not None}
    return jwt.encode(payload, key if key is not None else secret(), algorithm=algorithm)


def list_tasks_with(client, authorization):
    return client.get("/tasks", headers={"Authorization": authorization})


# --- Registration ---------------------------------------------------------------------------------


def test_register_creates_an_account_and_returns_only_public_fields(client, db):
    response = register(client, "Alice")

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"id", "username", "created_at"}
    assert body["username"] == "Alice"  # the case the user chose is kept
    assert datetime.fromisoformat(body["created_at"]).tzinfo is not None
    assert PASSWORD not in response.text and "argon2" not in response.text


def test_the_stored_password_is_an_argon2_hash_never_the_password(client, db):
    register(client, "alice")

    [user] = db.rows[User].values()
    assert user.password_hash != PASSWORD and PASSWORD not in user.password_hash
    assert user.password_hash.startswith("$argon2id$")
    assert security.verify_password(user.password_hash, PASSWORD)
    assert not security.verify_password(user.password_hash, PASSWORD + "x")


def test_the_same_password_is_stored_as_different_hashes(client, db):
    register(client, "alice")
    register(client, "bob")

    first, second = (user.password_hash for user in db.rows[User].values())
    assert first != second  # each hash has its own salt


@pytest.mark.parametrize("second", ["alice", "Alice", "ALICE", "aLiCe"])
def test_a_username_that_differs_only_in_case_is_taken(client, db, second):
    assert register(client, "alice").status_code == 201

    response = register(client, second, password="another good password")

    assert (response.status_code, response.json()) == (409, {"detail": "Username already taken"})
    assert len(db.rows[User]) == 1


def test_losing_a_registration_race_is_a_409_not_a_500(client, db):
    db.conflict_on_commit = True  # another request registered the name between our check and insert

    response = register(client, "alice")

    assert (response.status_code, response.json()) == (409, {"detail": "Username already taken"})


def test_other_integrity_errors_are_not_reported_as_a_taken_username(lenient, db, monkeypatch):
    class OtherViolation(Exception):
        sqlstate = "23514"  # a CHECK violation, not a duplicate

    def commit():
        raise IntegrityError("INSERT INTO users", {}, OtherViolation())

    monkeypatch.setattr(db, "commit", commit)

    response = register(lenient, "alice")

    assert response.status_code == 500
    assert response.text == "Internal Server Error"


@pytest.mark.parametrize("username", ["abc", "A_b-c.d", "a" * 32, "123", "User.Name-1_x"])
def test_valid_usernames_are_accepted(client, username):
    assert register(client, username).status_code == 201


@pytest.mark.parametrize(
    "username",
    [
        "",
        "ab",
        "a" * 33,
        "bad name",
        "bad\n",
        "bad\nname",
        " leading",
        "trailing ",
        "émile",
        "名前名前",
        "a/b",
        "ali@ce",
        "al;ce",
        "a\x00b",
    ],
)
def test_invalid_usernames_are_rejected(client, db, username):
    response = register(client, username)

    assert response.status_code == 422
    assert db.rows[User] == {}


def post_raw(client, path, body_text):
    """Send JSON text as is. (httpx itself refuses to encode a lone surrogate, but a real client can send one.)"""
    return client.post(path, content=body_text, headers={"content-type": "application/json"})


LONE_SURROGATE_REGISTRATION = '{"username": "alice", "password": "lone \\ud800 surrogate password"}'
LONE_SURROGATE_LOGIN = '{"username": "alice", "password": "lone \\ud800 surrogate password"}'


@pytest.mark.parametrize("password", ["", "short", "123456789", "x" * 129, "bad\x00password!"])
def test_invalid_passwords_are_rejected(lenient, db, password):
    response = register(lenient, "alice", password)

    assert response.status_code == 422
    assert db.rows[User] == {}


def test_a_password_with_a_lone_surrogate_is_a_422_never_a_500_from_hashing(lenient, db):
    response = post_raw(lenient, "/auth/register", LONE_SURROGATE_REGISTRATION)

    assert response.status_code == 422
    response.content.decode("utf-8")  # the error response itself is fully encodable
    assert db.rows[User] == {}


@pytest.mark.parametrize("password", ["x" * 10, "x" * 128, "  spaces are kept  ", "pässwörd \U0001f511 ok"])
def test_valid_passwords_are_accepted_exactly_as_typed(client, db, password):
    assert register(client, "alice", password).status_code == 201
    [user] = db.rows[User].values()

    assert security.verify_password(user.password_hash, password)  # not trimmed, not normalised


@pytest.mark.parametrize("extra", ["id", "role", "is_admin", "owner_id", "created_at", "password_hash"])
def test_registration_rejects_fields_it_does_not_define(client, db, extra):
    response = client.post("/auth/register", json={"username": "alice", "password": PASSWORD, extra: 1})

    assert response.status_code == 422
    assert db.rows[User] == {}


def test_registration_is_open_without_a_token(client):
    assert register(client).status_code == 201  # no Authorization header was sent


# --- Login ----------------------------------------------------------------------------------------


def test_login_returns_a_bearer_token_that_works(client):
    register(client)

    response = login(client)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"access_token", "token_type", "expires_in"}
    assert body["token_type"] == "bearer" and body["expires_in"] == 1800
    assert response.headers["cache-control"] == "no-store"
    assert list_tasks_with(client, f"Bearer {body['access_token']}").status_code == 200


def test_the_token_lasts_thirty_minutes_and_carries_only_the_needed_claims(client):
    user_id = register(client).json()["id"]

    claims = jwt.decode(login(client).json()["access_token"], secret(), algorithms=["HS256"])

    assert set(claims) == {"sub", "iat", "exp"}
    assert claims["sub"] == str(user_id)
    assert claims["exp"] - claims["iat"] == 30 * 60


def test_the_token_is_signed_with_hs256(client):
    register(client)

    header = jwt.get_unverified_header(login(client).json()["access_token"])

    assert header["alg"] == "HS256"


@pytest.mark.parametrize("name", ["alice", "ALICE", "Alice"])
def test_login_ignores_the_case_of_the_username(client, name):
    register(client, "alice")

    assert login(client, name).status_code == 200


def test_a_wrong_password_and_an_unknown_user_get_exactly_the_same_response(client):
    register(client)

    wrong_password = login(client, "alice", "this is not the password")
    unknown_user = login(client, "nobody", "this is not the password")

    assert wrong_password.status_code == unknown_user.status_code == 401
    assert wrong_password.json() == unknown_user.json() == BAD_LOGIN
    assert dict(wrong_password.headers) == dict(unknown_user.headers)
    assert wrong_password.headers["www-authenticate"] == "Bearer"


def test_an_unknown_user_costs_a_password_check_too(client, monkeypatch):
    # Otherwise a login for a name that does not exist would be measurably faster than one that does.
    register(client)
    checks = []
    real = security.verify_password
    monkeypatch.setattr("app.routers.auth.verify_password", lambda *a: checks.append("real") or real(*a))
    monkeypatch.setattr("app.security.verify_password", lambda *a: checks.append("dummy") or real(*a))

    login(client, "alice", "wrong password here")
    login(client, "nobody", "wrong password here")

    assert checks == ["real", "dummy"]


def test_login_rejects_an_empty_body_and_unknown_fields(client):
    assert client.post("/auth/login", json={}).status_code == 422
    assert client.post("/auth/login", json={"username": "a", "password": "b", "role": "admin"}).status_code == 422


def test_login_with_unencodable_or_nul_text_fails_cleanly(lenient):
    register(lenient)

    assert post_raw(lenient, "/auth/login", LONE_SURROGATE_LOGIN).status_code == 422
    assert login(lenient, "alice", "nul\x00inside").status_code == 401  # just a wrong password


def test_logging_in_does_not_change_the_stored_account(client, db):
    register(client)
    before = [(user.username, user.password_hash) for user in db.rows[User].values()]

    login(client)
    login(client, password="wrong password here")

    assert [(user.username, user.password_hash) for user in db.rows[User].values()] == before


# --- Password hashing -----------------------------------------------------------------------------


def test_production_hashing_parameters_meet_the_owasp_minimum():
    assert security.ARGON2_PARAMETERS["memory_cost"] >= 19456  # KiB
    assert security.ARGON2_PARAMETERS["time_cost"] >= 2
    assert security.ARGON2_PARAMETERS["parallelism"] >= 1

    encoded = PasswordHasher(**security.ARGON2_PARAMETERS).hash(PASSWORD)

    assert encoded.startswith("$argon2id$")
    assert "m=19456,t=2,p=1" in encoded


def test_the_application_hasher_is_built_from_those_parameters():
    # The tests swap in a cheap hasher for speed, so check the source that builds the real one.
    source = (Path(security.__file__)).read_text(encoding="utf-8")

    assert "password_hasher = PasswordHasher(**ARGON2_PARAMETERS)" in source


@pytest.mark.parametrize("garbage", ["", "not a hash", "$argon2id$v=19$m=1$broken", "plaintext-password-here"])
def test_verifying_against_a_garbage_hash_is_a_failure_not_a_crash(garbage):
    assert security.verify_password(garbage, PASSWORD) is False


# --- Access tokens --------------------------------------------------------------------------------


@pytest.fixture
def user(db):
    return db.add_user("alice")


def test_a_valid_token_is_accepted(client, user):
    assert list_tasks_with(client, f"Bearer {token()}").status_code == 200


def test_the_scheme_name_is_not_case_sensitive(client, user):
    assert list_tasks_with(client, f"bearer {token()}").status_code == 200


@pytest.mark.parametrize(
    "case, authorization",
    [
        ("no header at all", None),
        ("empty header", ""),
        ("scheme only", "Bearer"),
        ("scheme and a space", "Bearer "),
        ("another scheme", "Basic dXNlcjpwYXNz"),
        ("token without a scheme", "tokenwithoutscheme"),
        ("not a token", "Bearer not-a-token"),
        ("three dots", "Bearer a.b.c"),
        ("garbage segments", "Bearer !!!.@@@.###"),
    ],
)
def test_missing_or_malformed_credentials_are_401(client, user, case, authorization):
    headers = {} if authorization is None else {"Authorization": authorization}

    response = client.get("/tasks", headers=headers)

    assert response.status_code == 401, case
    assert response.json() == NOT_AUTHENTICATED
    assert response.headers["www-authenticate"] == "Bearer"


def test_a_truncated_token_is_401(client, user):
    assert list_tasks_with(client, f"Bearer {token()[:-5]}").status_code == 401
    assert list_tasks_with(client, f"Bearer {token().rsplit('.', 1)[0]}").status_code == 401


def test_an_expired_token_is_401(client, user):
    now = datetime.now(UTC)
    expired = token({"iat": now - timedelta(hours=1), "exp": now - timedelta(seconds=1)})

    response = list_tasks_with(client, f"Bearer {expired}")

    assert (response.status_code, response.json()) == (401, NOT_AUTHENTICATED)
    assert response.headers["www-authenticate"] == "Bearer"


def test_a_token_that_is_about_to_expire_still_works(client, user):
    now = datetime.now(UTC)

    assert list_tasks_with(client, f"Bearer {token({'exp': now + timedelta(seconds=30)})}").status_code == 200


@pytest.mark.parametrize("missing", ["sub", "exp", "iat"])
def test_a_token_without_a_required_claim_is_401(client, user, missing):
    assert list_tasks_with(client, f"Bearer {token({missing: None})}").status_code == 401


def test_a_token_signed_with_another_key_is_401(client, user):
    forged = token(key="an-attacker-key-that-is-long-enough-123456")

    assert list_tasks_with(client, f"Bearer {forged}").status_code == 401


def test_an_unsigned_token_is_401(client, user):
    unsigned = jwt.encode(
        {"sub": "1", "iat": datetime.now(UTC), "exp": datetime.now(UTC) + timedelta(minutes=5)}, None, algorithm="none"
    )

    assert list_tasks_with(client, f"Bearer {unsigned}").status_code == 401


@pytest.mark.parametrize("algorithm", ["HS384", "HS512"])
def test_a_token_with_a_different_algorithm_is_401_even_if_signed_with_the_right_key(client, user, algorithm):
    # The accepted algorithm is fixed by the server, never taken from the token.
    assert list_tasks_with(client, f"Bearer {token(algorithm=algorithm)}").status_code == 401


def test_a_tampered_payload_is_401(client, db, user):
    other = db.add_user("bob")
    header, payload, signature = token().split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    claims["sub"] = str(other.id)
    forged_payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()

    response = list_tasks_with(client, f"Bearer {header}.{forged_payload}.{signature}")

    assert response.status_code == 401  # the signature no longer matches


@pytest.mark.parametrize(
    "subject", ["abc", "0", "-1", "1.5", "", " 1", "1 ", "+1", "01", "1_0", "١", "1\n", 1, [1], {"id": 1}]
)
def test_a_token_with_a_bad_subject_is_401(client, user, subject):
    bad = jwt.encode(
        {"sub": subject, "iat": datetime.now(UTC), "exp": datetime.now(UTC) + timedelta(minutes=5)},
        secret(),
        algorithm="HS256",
    )

    assert list_tasks_with(client, f"Bearer {bad}").status_code == 401, repr(subject)


def test_a_token_issued_in_the_future_is_401(client, user):
    future = datetime.now(UTC) + timedelta(minutes=10)

    assert (
        list_tasks_with(client, f"Bearer {token({'iat': future, 'exp': future + timedelta(minutes=5)})}").status_code
        == 401
    )


def test_a_token_that_is_not_yet_valid_is_401(client, user):
    assert (
        list_tasks_with(client, f"Bearer {token({'nbf': datetime.now(UTC) + timedelta(minutes=10)})}").status_code
        == 401
    )


def test_a_token_for_an_account_that_does_not_exist_is_401(client, user):
    assert list_tasks_with(client, f"Bearer {token({'sub': '999'})}").status_code == 401


def test_a_token_signed_by_a_previous_secret_stops_working(client, user, monkeypatch):
    old = token()
    monkeypatch.setenv("SECRET_KEY", "a-brand-new-secret-key-that-is-long-enough-0123")
    get_auth_settings.cache_clear()  # as after a restart with a rotated key

    assert list_tasks_with(client, f"Bearer {old}").status_code == 401


def test_authentication_does_not_depend_on_the_users_name_or_password(client, user):
    assert list_tasks_with(client, bearer_headers(user.id)["Authorization"]).status_code == 200


# --- Nothing secret leaks -------------------------------------------------------------------------


def test_validation_errors_on_auth_routes_never_echo_what_was_submitted(client, db):
    secret_password = "SuperSecretValue-12345"
    attempts = [
        client.post("/auth/register", json={"username": "ab", "password": secret_password}),  # username too short
        client.post("/auth/register", json={"password": secret_password}),  # username missing
        client.post("/auth/register", json={"username": "alice", "password": secret_password, "role": "admin"}),
        client.post("/auth/register", json={"username": "alice", "password": secret_password[:5]}),
        client.post("/auth/login", json={"password": secret_password}),
        client.post("/auth/login", json={"username": "x", "password": secret_password, "extra": 1}),
        client.post(
            "/auth/register",
            content='{"username": "alice", "password": "' + secret_password,
            headers={"content-type": "application/json"},
        ),
    ]

    for response in attempts:
        assert response.status_code == 422
        assert secret_password not in response.text
        assert all("input" not in error for error in response.json()["detail"])


def test_unencodable_and_nul_passwords_are_rejected_without_echo_or_crash(lenient, db):
    for response, bad in [
        (post_raw(lenient, "/auth/register", LONE_SURROGATE_REGISTRATION), "surrogate password"),
        (register(lenient, "alice", "nul\x00inside"), "nul"),
        (register(lenient, "alice", "x" * 129), "x" * 129),
    ]:
        assert response.status_code == 422
        response.content.decode("utf-8")  # serialization did not fail
        assert bad not in response.text and "input" not in response.text


def test_the_other_endpoints_keep_echoing_input_as_before(client, user):
    response = client.post("/tasks", json={"title": ""}, headers=bearer_headers(user.id))

    assert response.json()["detail"][0]["input"] == ""  # only /auth is redacted


def test_passwords_hashes_and_tokens_never_reach_responses_or_logs(client, db, caplog):
    seen = []
    with caplog.at_level(logging.DEBUG):
        seen.append(register(client))
        issued = login(client)
        seen.append(login(client, password="the wrong password"))
        seen.append(login(client, "nobody", "the wrong password"))
        access_token = issued.json()["access_token"]
        seen.append(list_tasks_with(client, f"Bearer {access_token}"))
        seen.append(list_tasks_with(client, "Bearer not-a-token"))
        seen.append(client.get("/tasks"))
        seen.append(client.post("/auth/register", json={"username": "x", "password": "short"}))

    [stored] = db.rows[User].values()
    everything = " ".join(response.text for response in seen) + caplog.text
    assert PASSWORD not in everything and "the wrong password" not in everything
    assert stored.password_hash not in everything and "$argon2" not in everything
    assert access_token not in caplog.text  # the token appears only in the login response itself
    assert access_token not in " ".join(r.text for r in seen)
    assert secret() not in everything


def test_an_unexpected_error_while_registering_does_not_expose_the_password(lenient, monkeypatch, caplog):
    def explode(password):
        raise RuntimeError("boom")

    monkeypatch.setattr("app.routers.auth.hash_password", explode)

    with caplog.at_level(logging.DEBUG):
        response = register(lenient)

    assert response.status_code == 500
    assert response.text == "Internal Server Error"
    assert PASSWORD not in response.text and PASSWORD not in caplog.text
