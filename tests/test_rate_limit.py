"""Rate limiting of login and registration, and the switch that disables registration."""

import sys
import threading

import pytest
from conftest import reset_access_settings
from fakes import FakeSession
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.requests import Request

from app import rate_limit
from app.config import AccessSettings
from app.database import get_db
from app.main import app
from app.models import User
from app.rate_limit import RateLimiter, Rule, client_address

PASSWORD = "correct horse battery staple"
TOO_MANY = {"detail": "Too many requests. Try again later."}
ADDRESS_A = ("203.0.113.10", 50000)
ADDRESS_B = ("203.0.113.20", 50000)


class Clock:
    def __init__(self):
        self.value = 1000.0

    def advance(self, seconds):
        self.value += seconds


@pytest.fixture
def clock(monkeypatch):
    fake = Clock()
    monkeypatch.setattr(rate_limit, "now", lambda: fake.value)
    return fake


@pytest.fixture
def limits(monkeypatch):
    """Choose limits for one test, e.g. limits(LOGIN_USER_LIMIT=2)."""

    def choose(**values):
        for name, value in values.items():
            monkeypatch.setenv(name, str(value))
        reset_access_settings()

    return choose


@pytest.fixture
def db():
    session = FakeSession()
    app.dependency_overrides[get_db] = lambda: session
    yield session
    app.dependency_overrides.clear()


def client_at(address=ADDRESS_A, **kwargs):
    return TestClient(app, client=address, **kwargs)


def register(client, username="alice", password=PASSWORD):
    return client.post("/auth/register", json={"username": username, "password": password})


def login(client, username="alice", password=PASSWORD):
    return client.post("/auth/login", json={"username": username, "password": password})


# --- the limiter itself ----------------------------------------------------------------------------------------


def test_the_limiter_allows_up_to_the_limit_then_refuses(clock):
    limiter, rule = RateLimiter(), Rule(3, 60)

    assert [limiter.attempt([("k", rule)]) for _ in range(3)] == [None, None, None]
    assert limiter.attempt([("k", rule)]) is not None


def test_the_window_slides_so_old_attempts_expire_one_by_one(clock):
    limiter, rule = RateLimiter(), Rule(2, 60)
    limiter.attempt([("k", rule)])
    clock.advance(30)
    limiter.attempt([("k", rule)])

    assert limiter.attempt([("k", rule)]) == 30  # the first attempt leaves the window in 30 s
    clock.advance(30)
    assert limiter.attempt([("k", rule)]) is None  # exactly one slot has opened
    assert limiter.attempt([("k", rule)]) is not None


def test_retry_after_is_whole_seconds_and_at_least_one(clock):
    limiter, rule = RateLimiter(), Rule(1, 10)
    limiter.attempt([("k", rule)])
    clock.advance(9.2)

    assert limiter.attempt([("k", rule)]) == 1
    clock.advance(0.7)
    assert limiter.attempt([("k", rule)]) == 1  # 0.1 s left still reports 1, never 0


def test_keys_are_counted_separately(clock):
    limiter, rule = RateLimiter(), Rule(1, 60)

    assert limiter.attempt([("a", rule)]) is None
    assert limiter.attempt([("b", rule)]) is None
    assert limiter.attempt([("a", rule)]) is not None


def test_a_refused_attempt_is_not_recorded_against_any_key(clock):
    limiter = RateLimiter()
    wide, narrow = Rule(10, 60), Rule(1, 60)
    limiter.attempt([("ip", wide), ("user", narrow)])

    for _ in range(20):  # refused by "user"; must not use up "ip"
        assert limiter.attempt([("ip", wide), ("user", narrow)]) is not None

    assert limiter.attempt([("ip", wide), ("other", narrow)]) is None
    assert limiter._hits["ip"].__len__() == 2


def test_the_longest_wait_among_the_refusing_keys_is_reported(clock):
    limiter = RateLimiter()
    limiter.attempt([("short", Rule(1, 10)), ("long", Rule(1, 100))])

    assert limiter.attempt([("short", Rule(1, 10)), ("long", Rule(1, 100))]) == 100


def test_forget_clears_one_key_only(clock):
    limiter, rule = RateLimiter(), Rule(1, 60)
    limiter.attempt([("a", rule), ("b", rule)])

    limiter.forget("a")

    assert limiter.attempt([("a", rule)]) is None
    assert limiter.attempt([("b", rule)]) is not None


def test_memory_is_bounded_by_dropping_the_least_recently_used_keys(clock):
    limiter, rule = RateLimiter(max_keys=3), Rule(1, 60)
    for key in ("a", "b", "c"):
        limiter.attempt([(key, rule)])
    limiter.attempt([("d", rule)])  # evicts "a", the oldest

    assert list(limiter._hits) == ["b", "c", "d"]
    assert limiter.attempt([("a", rule)]) is None  # forgotten, so allowed again (the price of the bound)


def race(threads_count, limit):
    """Attempt from many threads at once; returns how many attempts were allowed."""
    limiter, rule = RateLimiter(), Rule(limit, 60)
    results = []
    barrier = threading.Barrier(threads_count)

    def attempt():
        barrier.wait()
        results.append(limiter.attempt([("k", rule)]))

    threads = [threading.Thread(target=attempt) for _ in range(threads_count)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    return results.count(None)


def test_concurrent_attempts_cannot_exceed_the_limit(clock):
    # Force the interpreter to switch threads as often as it can, so that a check-then-record race (several
    # attempts all seeing a free slot) would show up. Under the lock they are serialized.
    previous = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        assert [race(16, 5) for _ in range(30)] == [5] * 30
    finally:
        sys.setswitchinterval(previous)


# --- which address is limited -----------------------------------------------------------------------------------


def request_from(peer, forwarded=()):
    headers = [(b"x-forwarded-for", value.encode()) for value in forwarded]
    scope = {"type": "http", "headers": headers, "client": (peer, 1234) if peer else None, "method": "GET"}
    return Request(scope)


def test_the_connection_address_is_used_by_default(limits):
    assert client_address(request_from("198.51.100.7")) == "198.51.100.7"


def test_forwarded_headers_are_ignored_unless_proxies_are_trusted(limits):
    request = request_from("198.51.100.7", ["1.2.3.4"])

    assert client_address(request) == "198.51.100.7"


def test_a_header_from_an_untrusted_peer_is_ignored_even_when_proxies_are_configured(limits):
    limits(TRUSTED_PROXIES="10.0.0.0/8")

    assert client_address(request_from("198.51.100.7", ["1.2.3.4"])) == "198.51.100.7"


def test_a_trusted_proxy_may_name_the_client(limits):
    limits(TRUSTED_PROXIES="10.0.0.5")

    assert client_address(request_from("10.0.0.5", ["198.51.100.99"])) == "198.51.100.99"


def test_the_nearest_untrusted_address_wins_so_a_forged_prefix_does_not_help(limits):
    limits(TRUSTED_PROXIES="10.0.0.0/8")
    # The client sent "6.6.6.6"; our proxy appended the address it saw, then a second trusted proxy appended itself.
    request = request_from("10.0.0.2", ["6.6.6.6, 198.51.100.99, 10.0.0.1"])

    assert client_address(request) == "198.51.100.99"


def test_several_forwarded_headers_are_read_as_one_chain(limits):
    limits(TRUSTED_PROXIES="10.0.0.0/8")

    assert client_address(request_from("10.0.0.2", ["6.6.6.6", "198.51.100.99"])) == "198.51.100.99"


@pytest.mark.parametrize("chain", ["not-an-ip", "198.51.100.99, garbage", ""])
def test_a_garbled_chain_falls_back_to_the_proxy_address(limits, chain):
    limits(TRUSTED_PROXIES="10.0.0.5")

    assert client_address(request_from("10.0.0.5", [chain])) == "10.0.0.5"


def test_ipv6_clients_are_grouped_by_their_64_bit_prefix(limits):
    a = client_address(request_from("2001:db8:1:2:aaaa:bbbb:cccc:dddd"))
    b = client_address(request_from("2001:db8:1:2:1111:2222:3333:4444"))
    c = client_address(request_from("2001:db8:1:3::1"))

    assert a == b == "2001:db8:1:2::/64"
    assert c != a


def test_an_ipv4_mapped_address_counts_as_the_ipv4_address(limits):
    assert client_address(request_from("::ffff:198.51.100.7")) == "198.51.100.7"


def test_a_missing_client_address_does_not_crash(limits):
    assert client_address(request_from(None)) == "unknown"


# --- settings -----------------------------------------------------------------------------------------------------


def test_the_defaults_are_the_agreed_limits(monkeypatch):
    for name in ("LOGIN_IP_LIMIT", "LOGIN_USER_LIMIT", "REGISTER_IP_LIMIT", "REGISTRATION_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    settings = AccessSettings(_env_file=None)

    assert (settings.login_ip_limit, settings.login_ip_window_seconds) == (10, 60)
    assert (settings.login_user_limit, settings.login_user_window_seconds) == (5, 60)
    assert (settings.register_ip_limit, settings.register_ip_window_seconds) == (5, 3600)
    assert settings.registration_enabled is True
    assert settings.trusted_proxy_networks == ()


@pytest.mark.parametrize(
    "name, value",
    [
        ("LOGIN_IP_LIMIT", "0"),
        ("LOGIN_IP_LIMIT", "-1"),
        ("LOGIN_USER_LIMIT", "many"),
        ("LOGIN_USER_WINDOW_SECONDS", "0"),
        ("REGISTER_IP_WINDOW_SECONDS", "86401"),
        ("REGISTRATION_ENABLED", "maybe"),
        ("TRUSTED_PROXIES", "not-an-address"),
        ("TRUSTED_PROXIES", "10.0.0.1, 10.0.0.0/99"),
    ],
)
def test_invalid_access_settings_are_rejected(monkeypatch, name, value):
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError):
        AccessSettings(_env_file=None)


def test_trusted_proxies_accepts_addresses_and_networks_with_spaces(monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXIES", " 10.0.0.1 , 192.168.0.0/16,fd00::/8 ,")

    assert [str(n) for n in AccessSettings(_env_file=None).trusted_proxy_networks] == [
        "10.0.0.1/32",
        "192.168.0.0/16",
        "fd00::/8",
    ]


def test_the_server_refuses_to_start_with_invalid_access_settings(limits):
    limits(LOGIN_IP_LIMIT="0")

    with pytest.raises(ValidationError):
        with TestClient(app):
            pass


# --- login ----------------------------------------------------------------------------------------------------------


def test_login_is_refused_with_429_after_too_many_attempts_for_one_username(db, clock, limits):
    limits(LOGIN_USER_LIMIT=3)
    client = client_at()
    register(client)

    assert [login(client, password="wrong password").status_code for _ in range(3)] == [401, 401, 401]
    response = login(client, password="wrong password")

    assert response.status_code == 429
    assert response.json() == TOO_MANY
    assert response.headers["Retry-After"] == "60"


def test_the_correct_password_is_refused_too_while_limited_so_it_reveals_nothing(db, clock, limits):
    limits(LOGIN_USER_LIMIT=2)
    client = client_at()
    register(client)
    login(client, password="wrong password")
    login(client, password="wrong password")

    limited = login(client)  # right password, but over the limit

    assert limited.status_code == 429
    assert limited.json() == TOO_MANY
    assert "access_token" not in limited.text


def test_an_existing_and_a_missing_account_are_limited_identically(db, clock, limits):
    limits(LOGIN_USER_LIMIT=2)
    client = client_at()
    register(client)

    def exhaust(username):
        for _ in range(2):
            login(client, username, "wrong password")
        return login(client, username, "wrong password")

    real, missing = exhaust("alice"), exhaust("nobody")

    assert (real.status_code, real.json(), real.headers["Retry-After"]) == (
        missing.status_code,
        missing.json(),
        missing.headers["Retry-After"],
    )


def test_a_limited_request_does_no_database_work_and_no_password_hashing(db, clock, limits, monkeypatch):
    limits(LOGIN_USER_LIMIT=1)
    client = client_at()
    register(client)
    login(client, password="wrong password")

    def forbidden(*args, **kwargs):
        raise AssertionError("work done for a rate-limited request")

    monkeypatch.setattr("app.routers.auth.find_user", forbidden)
    monkeypatch.setattr("app.routers.auth.verify_password", forbidden)
    monkeypatch.setattr("app.routers.auth.burn_password_check_time", forbidden)

    assert login(client, password="wrong password").status_code == 429


def test_the_window_expiring_lets_login_work_again(db, clock, limits):
    limits(LOGIN_USER_LIMIT=2, LOGIN_USER_WINDOW_SECONDS=30)
    client = client_at()
    register(client)
    login(client, password="wrong password")
    login(client, password="wrong password")
    assert login(client).status_code == 429

    clock.advance(30)

    assert login(client).status_code == 200


def test_a_successful_login_after_a_rejected_attempt(db, clock, limits):
    limits(LOGIN_USER_LIMIT=3)
    client = client_at()
    register(client)

    assert login(client, password="wrong password").status_code == 401  # a failed attempt
    assert login(client).status_code == 200  # still within the limit: the right password works


def test_a_successful_login_clears_the_failures_for_that_username(db, clock, limits):
    limits(LOGIN_USER_LIMIT=3)
    client = client_at()
    register(client)
    login(client, password="wrong password")
    login(client, password="wrong password")
    assert login(client).status_code == 200

    # Without the reset, one more attempt would already be refused.
    assert [login(client, password="wrong password").status_code for _ in range(3)] == [401, 401, 401]
    assert login(client, password="wrong password").status_code == 429


def test_usernames_are_normalized_so_changing_case_does_not_escape_the_limit(db, clock, limits):
    limits(LOGIN_USER_LIMIT=3)
    client = client_at()
    register(client)

    for name in ("alice", "ALICE", "Alice"):
        assert login(client, name, "wrong password").status_code == 401

    assert login(client, "aLiCe", "wrong password").status_code == 429


def test_different_usernames_have_separate_username_limits(db, clock, limits):
    limits(LOGIN_USER_LIMIT=1)
    client = client_at()
    register(client, "alice")
    register(client, "bob")
    login(client, "alice", "wrong password")

    assert login(client, "alice", "wrong password").status_code == 429
    assert login(client, "bob", "wrong password").status_code == 401


def test_the_per_address_limit_covers_all_usernames(db, clock, limits):
    limits(LOGIN_IP_LIMIT=4)
    client = client_at()

    codes = [login(client, f"user{i}", "wrong password").status_code for i in range(5)]

    assert codes == [401, 401, 401, 401, 429]  # five different usernames, one address


def test_a_successful_login_does_not_reset_the_address_limit(db, clock, limits):
    limits(LOGIN_IP_LIMIT=3)
    client = client_at()
    register(client)

    assert [login(client).status_code for _ in range(3)] == [200, 200, 200]
    assert login(client).status_code == 429


def test_other_clients_are_not_affected(db, clock, limits):
    limits(LOGIN_USER_LIMIT=1, LOGIN_IP_LIMIT=2)
    first, second = client_at(ADDRESS_A), client_at(ADDRESS_B)
    register(first)
    login(first, password="wrong password")
    assert login(first, password="wrong password").status_code == 429

    assert login(second).status_code == 200  # a different address has its own budget, untouched by the first
    assert login(second).status_code == 200
    assert login(second).status_code == 429  # its own address limit (2) is now used up, and only its own


def test_a_forged_forwarded_header_does_not_escape_the_limit(db, clock, limits):
    limits(LOGIN_USER_LIMIT=2)
    client = client_at()
    register(client)

    codes = [
        client.post(
            "/auth/login",
            json={"username": "alice", "password": "wrong password"},
            headers={"X-Forwarded-For": f"9.9.9.{i}"},
        ).status_code
        for i in range(4)
    ]

    assert codes == [401, 401, 429, 429]


def test_behind_a_trusted_proxy_clients_are_told_apart_by_the_forwarded_header(db, clock, limits):
    limits(LOGIN_USER_LIMIT=1, TRUSTED_PROXIES="203.0.113.10")
    proxy = client_at(ADDRESS_A)
    register(proxy)

    def attempt(client_ip):
        return proxy.post(
            "/auth/login",
            json={"username": "alice", "password": "wrong password"},
            headers={"X-Forwarded-For": client_ip},
        ).status_code

    assert attempt("198.51.100.1") == 401
    assert attempt("198.51.100.1") == 429
    assert attempt("198.51.100.2") == 401  # another client behind the same proxy


def test_requests_with_an_invalid_body_do_not_use_up_attempts(db, clock, limits):
    limits(LOGIN_USER_LIMIT=1)
    client = client_at()
    register(client)

    for _ in range(5):
        assert client.post("/auth/login", json={"username": "alice"}).status_code == 422

    assert login(client, password="wrong password").status_code == 401


# --- registration -------------------------------------------------------------------------------------------------


def test_registration_is_limited_per_address(db, clock, limits):
    limits(REGISTER_IP_LIMIT=2)
    client = client_at()

    codes = [register(client, f"user{i}").status_code for i in range(3)]
    response = register(client, "user9")

    assert codes == [201, 201, 429]
    assert response.json() == TOO_MANY
    assert response.headers["Retry-After"] == "3600"


def test_registration_attempts_that_fail_still_count(db, clock, limits):
    limits(REGISTER_IP_LIMIT=2)
    client = client_at()
    register(client, "alice")

    assert register(client, "alice").status_code == 409
    assert register(client, "carol").status_code == 429


def test_registration_limits_are_separate_per_client(db, clock, limits):
    limits(REGISTER_IP_LIMIT=1)
    register(client_at(ADDRESS_A), "alice")

    assert register(client_at(ADDRESS_A), "bob").status_code == 429
    assert register(client_at(ADDRESS_B), "bob").status_code == 201


def test_registration_works_again_after_the_window(db, clock, limits):
    limits(REGISTER_IP_LIMIT=1, REGISTER_IP_WINDOW_SECONDS=120)
    client = client_at()
    register(client, "alice")
    assert register(client, "bob").status_code == 429

    clock.advance(120)

    assert register(client, "bob").status_code == 201


def test_registration_and_login_limits_do_not_affect_each_other(db, clock, limits):
    limits(REGISTER_IP_LIMIT=1, LOGIN_IP_LIMIT=1)
    client = client_at()
    register(client)

    assert register(client, "bob").status_code == 429
    assert login(client).status_code == 200


def test_disabled_registration_is_403_and_login_still_works(db, clock, limits):
    client = client_at()
    register(client)
    limits(REGISTRATION_ENABLED="false")

    refused = register(client, "bob")

    assert (refused.status_code, refused.json()) == (403, {"detail": "Registration is disabled"})
    assert login(client).status_code == 200
    assert login(client, password="wrong password").status_code == 401


def test_disabled_registration_creates_nothing_and_ignores_the_body(db, limits):
    limits(REGISTRATION_ENABLED="false")
    client = client_at()

    assert register(client, "bob").status_code == 403
    assert client.post("/auth/register", json={"nonsense": True}).status_code == 403
    assert client.post("/auth/register", content=b"not json").status_code == 403
    assert db.rows[User] == {}


def test_disabled_registration_does_not_use_up_the_rate_limit(db, clock, limits):
    limits(REGISTRATION_ENABLED="false", REGISTER_IP_LIMIT=1)
    client = client_at()
    for _ in range(3):
        assert register(client, "bob").status_code == 403

    limits(REGISTRATION_ENABLED="true", REGISTER_IP_LIMIT=1)
    assert register(client, "bob").status_code == 201


@pytest.mark.parametrize("value", ["false", "False", "0", "no", "off"])
def test_registration_can_be_switched_off_in_the_usual_ways(db, limits, value):
    limits(REGISTRATION_ENABLED=value)

    assert register(client_at()).status_code == 403


# --- the API description -------------------------------------------------------------------------------------------


def test_openapi_documents_the_new_responses():
    spec = TestClient(app).get("/openapi.json").json()["paths"]

    assert {"403", "409", "429", "201", "422"} <= set(spec["/auth/register"]["post"]["responses"])
    assert {"401", "429", "200", "422"} <= set(spec["/auth/login"]["post"]["responses"])
    login_body = spec["/auth/login"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert login_body == {"$ref": "#/components/schemas/LoginRequest"}
