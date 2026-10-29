import asyncio
import json

import pytest
from fakes import FakeSession
from fastapi.testclient import TestClient

from app.body_limit import MAX_REQUEST_BODY_BYTES, BodySizeLimitMiddleware
from app.database import get_db
from app.main import app

LIMIT = MAX_REQUEST_BODY_BYTES
TOO_LARGE = {"detail": "Request body too large"}


async def read_whole_body(scope, receive, send):
    """A minimal application: reads the entire request body, then answers 200 with its size."""
    size = 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            raise RuntimeError("client went away")  # what a framework does on a disconnect
        size += len(message.get("body", b""))
        if not message.get("more_body", False):
            break
    body = str(size).encode()
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-length", b"%d" % len(body))]})
    await send({"type": "http.response.body", "body": body})


def drive(chunks, headers=(), application=read_whole_body, max_bytes=LIMIT):
    """Run the middleware over the ASGI protocol. Returns (sent messages, chunks pulled from the client)."""
    scope = {"type": "http", "method": "POST", "path": "/", "headers": list(headers)}
    pending = list(chunks)
    pulled = 0
    sent = []

    async def receive():
        nonlocal pulled
        if not pending:
            return {"type": "http.disconnect"}
        pulled += 1
        body = pending.pop(0)
        return {"type": "http.request", "body": body, "more_body": bool(pending)}

    async def send(message):
        sent.append(message)

    try:
        asyncio.run(BodySizeLimitMiddleware(application, max_bytes)(scope, receive, send))
    except RuntimeError:
        pass  # the stand-in application gives up on the disconnect; only what was sent matters
    return sent, pulled


def status_and_body(sent):
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], body


def length(n):
    return [(b"content-length", str(n).encode())]


# --- through the real application ------------------------------------------------------------------------


@pytest.fixture
def client():
    app.dependency_overrides[get_db] = lambda: FakeSession()
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_a_normal_request_still_reaches_the_application(client):
    response = client.post("/auth/register", json={"username": "alice", "password": "correct horse battery staple"})

    assert response.status_code == 201


def test_a_body_over_the_limit_is_refused_with_413(client):
    response = client.post("/auth/register", content=b"x" * (LIMIT + 1), headers={"Content-Type": "application/json"})

    assert response.status_code == 413
    assert response.json() == TOO_LARGE


def test_oversized_task_bodies_are_refused_before_authentication(client):
    response = client.post("/tasks", content=b"x" * (LIMIT + 1), headers={"Content-Type": "application/json"})

    assert response.status_code == 413


def test_a_chunked_body_over_the_limit_is_refused_with_413(client):
    def chunks():
        for _ in range(LIMIT // 1024 + 2):
            yield b"x" * 1024

    request = client.build_request("POST", "/auth/register", content=chunks())
    assert "content-length" not in request.headers  # httpx sends this body chunked

    response = client.send(request)

    assert response.status_code == 413
    assert response.json() == TOO_LARGE


def test_requests_without_a_body_are_untouched(client):
    assert client.get("/health").status_code == 200


# --- the protocol-level cases -------------------------------------------------------------------------------


def test_a_body_exactly_at_the_limit_is_accepted():
    sent, _ = drive([b"x" * LIMIT], length(LIMIT))

    assert status_and_body(sent) == (200, str(LIMIT).encode())


def test_one_byte_over_the_limit_is_refused():
    sent, _ = drive([b"x" * (LIMIT + 1)], length(LIMIT + 1))

    assert status_and_body(sent) == (413, json.dumps(TOO_LARGE).encode())


def test_a_declared_length_over_the_limit_is_refused_without_reading_the_body():
    sent, pulled = drive([b"x" * 10], length(10**12))

    assert status_and_body(sent)[0] == 413
    assert pulled == 0


def test_a_missing_content_length_is_counted_as_the_body_arrives():
    sent, _ = drive([b"x" * 1000] * 3)  # no Content-Length header at all

    assert status_and_body(sent) == (200, b"3000")


def test_without_content_length_the_limit_still_applies_and_reading_stops_early():
    chunk = 8 * 1024
    chunks = [b"x" * chunk] * 100  # 800 KiB on offer
    sent, pulled = drive(chunks)

    assert status_and_body(sent)[0] == 413
    assert pulled == LIMIT // chunk + 1  # stopped on the first chunk that crossed the limit, not after all 100


def test_a_content_length_that_understates_the_body_cannot_get_past_the_limit():
    sent, pulled = drive([b"x" * 1024] * 100, length(10))

    assert status_and_body(sent)[0] == 413
    assert pulled < 100


def test_a_content_length_that_overstates_a_small_body_is_not_refused():
    sent, _ = drive([b"x" * 10], length(LIMIT))  # within the limit either way; the server enforces framing

    assert status_and_body(sent) == (200, b"10")


def test_exactly_one_response_is_sent_when_the_limit_is_crossed_mid_stream():
    sent, _ = drive([b"x" * 1024] * 100, max_bytes=2048)

    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]


def test_the_applications_own_reply_to_the_cut_off_body_is_dropped():
    async def answers_even_after_the_disconnect(scope, receive, send):
        while (await receive())["type"] == "http.request":
            pass
        await send({"type": "http.response.start", "status": 500, "headers": []})
        await send({"type": "http.response.body", "body": b"boom"})

    sent, _ = drive([b"x" * 1024] * 100, application=answers_even_after_the_disconnect, max_bytes=2048)

    assert status_and_body(sent)[0] == 413


@pytest.mark.parametrize("digits", [19, 5000])
def test_a_content_length_with_a_huge_number_of_digits_is_refused_not_a_crash(digits):
    sent, pulled = drive([b"x"], length("9" * digits))

    assert status_and_body(sent)[0] == 413
    assert pulled == 0


def test_leading_zeros_do_not_change_the_declared_length():
    assert status_and_body(drive([b"x" * 10], length("0" * 5000 + "10"))[0]) == (200, b"10")
    assert status_and_body(drive([b"x"], length("0" * 5000 + str(LIMIT + 1)))[0])[0] == 413


@pytest.mark.parametrize("value", [b"abc", b"-1", b"", b"1e3", b" 5", b"\xd9\xa5"])
def test_a_malformed_content_length_is_a_400(value):
    sent, pulled = drive([b"x"], [(b"content-length", value)])

    assert status_and_body(sent)[0] == 400
    assert pulled == 0


@pytest.mark.parametrize("order", [(1, LIMIT + 1), (LIMIT + 1, 1)])
def test_conflicting_content_lengths_are_judged_by_the_largest(order):
    sent, _ = drive([b"x"], [(b"content-length", str(n).encode()) for n in order])

    assert status_and_body(sent)[0] == 413


def test_non_http_scopes_pass_through_untouched():
    seen = []

    async def application(scope, receive, send):
        seen.append(scope["type"])

    asyncio.run(BodySizeLimitMiddleware(application)({"type": "lifespan"}, None, None))

    assert seen == ["lifespan"]


def test_the_limit_is_64_kib_and_installed_on_the_application():
    assert LIMIT == 64 * 1024
    assert any(m.cls is BodySizeLimitMiddleware for m in app.user_middleware)
