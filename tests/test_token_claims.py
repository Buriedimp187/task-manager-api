"""Strict validation of access-token claims, and the start-up initialization of the dummy password hash.

Tokens are signed by hand with the real key, so each one is rejected (or not) on its claims alone.
"""

import base64
import hashlib
import hmac
import json
import subprocess
import sys
from pathlib import Path

import jwt
import pytest
from argon2 import PasswordHasher, extract_parameters
from fakes import FakeSession
from fastapi.testclient import TestClient

from app import security
from app.config import get_auth_settings
from app.database import get_db
from app.main import app
from app.security import ACCESS_TOKEN_TTL, MAX_USER_ID, decode_access_token

NOW = 1_800_000_000
TTL = int(ACCESS_TOKEN_TTL.total_seconds())
NOT_AUTHENTICATED = {"detail": "Not authenticated"}
PROJECT_ROOT = Path(__file__).resolve().parent.parent
REAL_NOW = security._now


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(security, "_now", lambda: NOW)


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def signed(payload, header=None, key=None):
    """A correctly signed HS256 token carrying exactly this payload (a dict, or raw JSON text)."""
    key = key or get_auth_settings().secret_key.get_secret_value()
    body = payload if isinstance(payload, str) else json.dumps(payload)
    signing_input = f"{b64(json.dumps(header or {'alg': 'HS256', 'typ': 'JWT'}).encode())}.{b64(body.encode())}"
    return f"{signing_input}.{b64(hmac.new(key.encode(), signing_input.encode(), hashlib.sha256).digest())}"


def claims(**overrides):
    base = {"sub": "7", "iat": NOW, "exp": NOW + TTL}
    base.update(overrides)
    return {key: value for key, value in base.items() if value is not ...}  # `...` removes a claim


def decode(token):
    return decode_access_token(token, get_auth_settings())


# --- valid tokens ---------------------------------------------------------------------------------


def test_a_token_made_by_the_application_is_accepted(monkeypatch):
    monkeypatch.setattr(security, "_now", REAL_NOW)  # the real clock, for a token made with the real clock
    token = security.create_access_token(42, get_auth_settings())

    assert decode(token) == 42


def test_created_tokens_carry_exactly_the_expected_integer_claims():
    token = security.create_access_token(5, get_auth_settings())

    payload = jwt.decode(token, options={"verify_signature": False})

    assert set(payload) == {"sub", "iat", "exp"}
    assert payload["sub"] == "5"
    assert type(payload["iat"]) is int and type(payload["exp"]) is int
    assert payload["exp"] - payload["iat"] == TTL


def test_a_hand_signed_token_with_proper_claims_is_accepted():
    assert decode(signed(claims())) == 7


@pytest.mark.parametrize("user_id", ["1", "7", "2147483647", "1000000"])
def test_subjects_that_fit_the_id_column_are_accepted(user_id):
    assert decode(signed(claims(sub=user_id))) == int(user_id)


# --- the signature and the algorithm --------------------------------------------------------------


@pytest.mark.parametrize(
    "make_token",
    [
        lambda: signed(claims(), key="x" * 40),  # another key
        lambda: signed(claims(), header={"alg": "HS384", "typ": "JWT"}),  # a different algorithm, same key
        lambda: signed(claims(), header={"alg": "none", "typ": "JWT"}).rsplit(".", 1)[0] + ".",  # unsigned
        lambda: "not-a-token",
        lambda: "",
        lambda: "a.b.c",
    ],
)
def test_tokens_that_are_not_correctly_signed_with_hs256_are_rejected(make_token):
    assert decode(make_token()) is None


def test_a_tampered_payload_keeps_the_old_signature_and_is_rejected():
    head, _, signature = signed(claims()).split(".")

    assert decode(f"{head}.{b64(json.dumps(claims(sub='8')).encode())}.{signature}") is None


# --- structurally odd tokens ----------------------------------------------------------------------------------------


def raw_token(header: bytes, payload: bytes, key=None):
    """A correctly signed token whose header and payload are arbitrary bytes."""
    key = key or get_auth_settings().secret_key.get_secret_value()
    signing_input = f"{b64(header)}.{b64(payload)}"
    return f"{signing_input}.{b64(hmac.new(key.encode(), signing_input.encode(), hashlib.sha256).digest())}"


GOOD_HEADER = b'{"alg":"HS256","typ":"JWT"}'
GOOD_PAYLOAD = json.dumps(claims()).encode()


@pytest.mark.parametrize(
    "header, payload",
    [
        (b"[" * 12000, GOOD_PAYLOAD),  # nested far deeper than the JSON parser allows
        (GOOD_HEADER, b"[" * 12000),
        (b'{"alg":"HS256","x":' + b"9" * 6000 + b"}", GOOD_PAYLOAD),
        (GOOD_HEADER, b"[]"),  # valid JSON, but not an object
        (GOOD_HEADER, b"7"),
        (GOOD_HEADER, b'"text"'),
        (GOOD_HEADER, b"null"),
        (GOOD_HEADER, bytes([0xFF, 0xFE]) + b" not utf-8"),
        (GOOD_HEADER, b""),
        (b"[]", GOOD_PAYLOAD),
        (b"", GOOD_PAYLOAD),
        (b'{"alg":"HS256","crit":["x"],"x":1}', GOOD_PAYLOAD),  # a critical extension nobody supports
    ],
)
def test_structurally_odd_tokens_are_rejected_without_an_error(header, payload):
    token = raw_token(header, payload)

    assert decode(token) is None
    assert decode(token.replace(".", ".x", 1)) is None  # and so is the same token with a damaged middle


def test_the_baseline_raw_token_is_valid():
    assert decode(raw_token(GOOD_HEADER, GOOD_PAYLOAD)) == 7  # so the rejections above are about the odd parts


# --- missing claims -------------------------------------------------------------------------------


@pytest.mark.parametrize("missing", ["sub", "iat", "exp"])
def test_a_missing_required_claim_is_rejected(missing):
    assert decode(signed(claims(**{missing: ...}))) is None


# --- exp and iat ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [str(NOW + TTL), float(NOW + TTL), NOW + TTL + 0.5, True, False, None, [NOW + TTL], {"v": 1}, "soon", ""],
)
def test_exp_must_be_an_integer(value):
    assert decode(signed(claims(exp=value))) is None


@pytest.mark.parametrize("value", [str(NOW), float(NOW), NOW + 0.5, True, False, None, [NOW], {"v": 1}, "now", ""])
def test_iat_must_be_an_integer(value):
    assert decode(signed(claims(iat=value))) is None


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_non_finite_numbers_are_rejected(literal):
    assert decode(signed(f'{{"sub":"7","iat":{NOW},"exp":{literal}}}')) is None
    assert decode(signed(f'{{"sub":"7","iat":{literal},"exp":{NOW + TTL}}}')) is None


def test_an_expired_token_is_rejected_and_the_boundary_is_exact():
    assert decode(signed(claims(iat=NOW - TTL + 1, exp=NOW + 1))) == 7  # one second left
    assert decode(signed(claims(iat=NOW - TTL + 1, exp=NOW))) is None  # expires this very second
    assert decode(signed(claims(iat=NOW - TTL - 1, exp=NOW - 1))) is None


def test_a_token_issued_in_the_future_is_rejected_beyond_a_small_clock_skew():
    skew = security.IAT_LEEWAY_SECONDS

    assert decode(signed(claims(iat=NOW + skew, exp=NOW + skew + TTL))) == 7
    assert decode(signed(claims(iat=NOW + skew + 1, exp=NOW + skew + 1 + TTL))) is None
    assert decode(signed(claims(iat=NOW + 3600, exp=NOW + 3600 + TTL))) is None


def test_exp_must_come_after_iat():
    assert decode(signed(claims(iat=NOW, exp=NOW))) is None
    assert decode(signed(claims(iat=NOW, exp=NOW - 5))) is None
    skew = 3  # inside the clock-skew window, where exp is still in the future
    assert decode(signed(claims(iat=NOW + skew, exp=NOW + skew - 1))) is None
    assert decode(signed(claims(iat=NOW + skew, exp=NOW + skew))) is None
    assert decode(signed(claims(iat=NOW - 10, exp=NOW + 1))) == 7


def test_a_token_may_not_live_longer_than_the_server_issues():
    assert decode(signed(claims(iat=NOW, exp=NOW + TTL))) == 7
    assert decode(signed(claims(iat=NOW, exp=NOW + TTL + 1))) is None
    assert decode(signed(claims(iat=NOW, exp=NOW + 31_536_000))) is None


def test_negative_and_huge_timestamps_are_rejected():
    assert decode(signed(claims(iat=-5, exp=NOW + TTL))) is None
    assert decode(signed(claims(iat=NOW, exp=10**30))) is None
    assert decode(signed(claims(iat=10**30, exp=10**30 + 1))) is None


# --- sub ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "subject",
    [
        "0",
        "-1",
        "+1",
        "01",
        " 1",
        "1 ",
        "1\n",
        "1.0",
        "1e3",
        "0x1",
        "",
        "abc",
        "１２",  # full-width digits
        "٣",  # Arabic-Indic digit
        "2147483648",  # one past the id column
        "9" * 11,
        "9" * 20,
        "9" * 5000,  # int() itself refuses this many digits
        1,
        1.0,
        True,
        None,
        ["1"],
        {"id": 1},
    ],
)
def test_invalid_subjects_are_rejected(subject):
    assert decode(signed(claims(sub=subject))) is None


def test_the_largest_valid_subject_is_the_id_column_maximum():
    assert MAX_USER_ID == 2_147_483_647
    assert decode(signed(claims(sub=str(MAX_USER_ID)))) == MAX_USER_ID


# --- unexpected claims ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [{"nbf": NOW}, {"iss": "x"}, {"aud": "x"}, {"jti": "1"}, {"admin": True}, {"role": "admin"}, {"scope": "all"}],
)
def test_tokens_with_extra_claims_are_rejected(extra):
    assert decode(signed({**claims(), **extra})) is None


# --- through the API: every failure is the same 401 -----------------------------------------------


@pytest.fixture
def db():
    session = FakeSession()
    app.dependency_overrides[get_db] = lambda: session
    yield session
    app.dependency_overrides.clear()


@pytest.mark.parametrize(
    "make_token",
    [
        lambda: signed(claims(exp=str(NOW + TTL))),
        lambda: signed(claims(iat=True)),
        lambda: signed(claims(sub="9" * 5000)),
        lambda: signed(claims(sub="2147483648")),
        lambda: signed({**claims(), "admin": True}),
        lambda: signed(claims(exp=NOW + 10**9)),
        lambda: signed(claims(exp=NOW)),
    ],
)
def test_a_bad_claim_gets_the_ordinary_401_and_never_reaches_the_database(db, make_token):
    db.add_user("alice")
    client = TestClient(app, headers={"Authorization": f"Bearer {make_token()}"})

    response = client.get("/tasks")

    assert (response.status_code, response.json()) == (401, NOT_AUTHENTICATED)
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert not any(call.startswith("get:User") for call in db.calls)  # rejected before any lookup


def test_a_valid_token_still_works_end_to_end(db):
    user = db.add_user("alice")
    token = signed(claims(sub=str(user.id)))

    response = TestClient(app, headers={"Authorization": f"Bearer {token}"}).get("/tasks")

    assert response.status_code == 200


def test_a_token_from_the_login_endpoint_works_end_to_end(db, monkeypatch):
    monkeypatch.setattr(security, "_now", REAL_NOW)  # the login endpoint stamps tokens with the real clock
    db.add_user("alice", password="the right password")
    client = TestClient(app)
    token = client.post("/auth/login", json={"username": "alice", "password": "the right password"}).json()[
        "access_token"
    ]

    assert client.get("/tasks", headers={"Authorization": f"Bearer {token}"}).status_code == 200


# --- the dummy password hash ----------------------------------------------------------------------


def test_nothing_is_hashed_when_the_application_is_imported():
    code = (
        f"import sys; sys.path.insert(0, {str(PROJECT_ROOT)!r}); "
        "import app.main, app.security as s; print(s._dummy_password_hash)"
    )
    result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)

    assert result.stdout.strip() == "None", result.stderr


def test_checking_before_initialization_fails_loudly_instead_of_hashing_lazily(monkeypatch):
    monkeypatch.setattr(security, "_dummy_password_hash", None)

    with pytest.raises(RuntimeError, match="not initialized"):
        security.burn_password_check_time("whatever")

    assert security._dummy_password_hash is None  # and it did not quietly create one


def test_initialization_creates_a_hash_with_the_current_parameters():
    security._dummy_password_hash = None

    security.initialize_dummy_password_hash()

    dummy = security._dummy_password_hash
    assert dummy.startswith("$argon2id$")
    assert not security.password_hasher.check_needs_rehash(dummy)
    assert security.verify_password(dummy, "a password nobody uses") is False


def test_the_dummy_hash_uses_the_parameters_of_the_hasher_that_makes_real_hashes(monkeypatch):
    # Verifying costs what the hash's own parameters say, so a dummy made with weaker ones would be faster than a
    # real check. A distinctive hasher shows which parameters the dummy really gets.
    distinctive = PasswordHasher(time_cost=2, memory_cost=16, parallelism=1)
    monkeypatch.setattr(security, "password_hasher", distinctive)

    security.initialize_dummy_password_hash()

    parameters = extract_parameters(security._dummy_password_hash)
    assert (parameters.time_cost, parameters.memory_cost, parameters.parallelism) == (2, 16, 1)


def test_production_hashing_uses_the_documented_parameters():
    assert security.ARGON2_PARAMETERS == {"memory_cost": 19456, "time_cost": 2, "parallelism": 1}


def test_each_initialization_uses_a_fresh_random_password():
    security.initialize_dummy_password_hash()
    first = security._dummy_password_hash

    security.initialize_dummy_password_hash()

    assert security._dummy_password_hash != first


def test_starting_the_application_initializes_the_dummy_hash(monkeypatch):
    monkeypatch.setattr(security, "_dummy_password_hash", None)

    with TestClient(app):
        assert security._dummy_password_hash is not None
        assert security._dummy_password_hash.startswith("$argon2id$")


def test_a_login_for_an_unknown_user_checks_the_startup_hash_and_is_a_plain_401(db, monkeypatch):
    checked = []
    real = security.verify_password
    monkeypatch.setattr(
        "app.security.verify_password", lambda hashed, password: checked.append(hashed) or real(hashed, password)
    )
    client = TestClient(app)

    first = client.post("/auth/login", json={"username": "nobody", "password": "some password"})
    second = client.post("/auth/login", json={"username": "other", "password": "some password"})

    assert (first.status_code, first.json()) == (401, {"detail": "Incorrect username or password"})
    assert (second.status_code, second.json()) == (401, first.json())
    assert checked == [security._dummy_password_hash] * 2  # the same hash, made before either request


def test_unknown_and_wrong_password_logins_look_identical(db):
    db.add_user("alice", password="the right password")
    client = TestClient(app)

    unknown = client.post("/auth/login", json={"username": "nobody", "password": "wrong password"})
    wrong = client.post("/auth/login", json={"username": "alice", "password": "wrong password"})

    assert (unknown.status_code, unknown.json(), dict(unknown.headers)) == (
        wrong.status_code,
        wrong.json(),
        dict(wrong.headers),
    )
