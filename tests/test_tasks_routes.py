"""The task endpoints' contract, for an authenticated user, against the in-memory fake database."""

from datetime import datetime

import pytest
from fakes import FakeSession, bearer_headers
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import UNENCODABLE_TEXT, app, without_unencodable_text
from app.routers import tasks
from app.schemas import TaskStatus

NOT_FOUND = {"detail": "Task not found"}


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
def client(alice):
    return TestClient(app, headers=bearer_headers(alice.id))


def create(client, **fields):
    response = client.post("/tasks", json={"title": "A task", **fields})
    assert response.status_code == 201
    return response.json()


def writes(db):
    """What the request did to the database, ignoring the user lookup that authentication performs."""
    return [call for call in db.calls if not call.startswith("get:User")]


# --- OpenAPI contract ----------------------------------------------------------------------------


@pytest.fixture
def spec(client):
    return client.get("/openapi.json").json()


def operation(spec, method, path):
    return spec["paths"][path][method]


def schema_ref(response):
    return response["content"]["application/json"]["schema"]


def test_openapi_lists_exactly_the_five_task_operations(spec):
    assert set(spec["paths"]["/tasks"]) == {"post", "get"}
    assert set(spec["paths"]["/tasks/{task_id}"]) == {"get", "patch", "delete"}


def test_openapi_documents_the_success_responses(spec):
    created = operation(spec, "post", "/tasks")["responses"]["201"]
    listed = operation(spec, "get", "/tasks")["responses"]["200"]

    assert schema_ref(created) == {"$ref": "#/components/schemas/TaskRead"}
    assert schema_ref(listed)["type"] == "array"  # FastAPI also adds a generated title
    assert schema_ref(listed)["items"] == {"$ref": "#/components/schemas/TaskRead"}
    for method in ("get", "patch"):
        assert schema_ref(operation(spec, method, "/tasks/{task_id}")["responses"]["200"]) == {
            "$ref": "#/components/schemas/TaskRead"
        }
    assert "content" not in operation(spec, "delete", "/tasks/{task_id}")["responses"]["204"]


def test_openapi_no_longer_documents_the_temporary_501(spec):
    assert "NotImplementedResponse" not in spec["components"]["schemas"]
    for methods in spec["paths"].values():
        for item in methods.values():
            assert "501" not in item["responses"]


def test_openapi_documents_404_exactly_where_a_task_is_looked_up(spec):
    for method in ("get", "patch", "delete"):
        responses = operation(spec, method, "/tasks/{task_id}")["responses"]
        assert schema_ref(responses["404"]) == {"$ref": "#/components/schemas/ErrorResponse"}
    assert "404" not in operation(spec, "post", "/tasks")["responses"]
    assert "404" not in operation(spec, "get", "/tasks")["responses"]


def test_openapi_documents_401_and_503_on_every_task_route(spec):
    for path, methods in spec["paths"].items():
        if not path.startswith("/tasks"):
            continue
        for method, item in methods.items():
            for status in ("401", "503"):
                assert schema_ref(item["responses"][status]) == {"$ref": "#/components/schemas/ErrorResponse"}, (
                    method,
                    path,
                    status,
                )


def test_openapi_declares_bearer_authentication_on_task_routes_only(spec):
    assert spec["components"]["securitySchemes"]["HTTPBearer"]["scheme"] == "bearer"
    for path, methods in spec["paths"].items():
        for method, item in methods.items():
            requires_token = bool(item.get("security"))
            assert requires_token == path.startswith("/tasks"), (method, path)


def test_openapi_error_response_shape(spec):
    schema = spec["components"]["schemas"]["ErrorResponse"]

    assert schema["required"] == ["detail"]
    assert schema["properties"]["detail"]["type"] == "string"


def test_openapi_documents_422_only_where_there_is_input_to_validate(spec):
    for method, path in [
        ("get", "/tasks"),  # limit and offset
        ("post", "/tasks"),
        ("get", "/tasks/{task_id}"),
        ("patch", "/tasks/{task_id}"),
        ("delete", "/tasks/{task_id}"),
    ]:
        assert "422" in operation(spec, method, path)["responses"]


def test_openapi_list_endpoint_documents_pagination(spec):
    listing = operation(spec, "get", "/tasks")
    parameters = {p["name"]: p for p in listing["parameters"]}

    assert set(parameters) == {"limit", "offset"}
    assert parameters["limit"]["schema"]["default"] == 50
    assert (parameters["limit"]["schema"]["minimum"], parameters["limit"]["schema"]["maximum"]) == (1, 200)
    assert (parameters["offset"]["schema"]["default"], parameters["offset"]["schema"]["minimum"]) == (0, 0)
    assert "X-Total-Count" in listing["responses"]["200"]["headers"]


def test_openapi_documents_the_task_limit_on_creation_only(spec):
    assert "409" in operation(spec, "post", "/tasks")["responses"]
    assert "409" not in operation(spec, "get", "/tasks")["responses"]


def test_openapi_documents_the_id_bounds(spec):
    [parameter] = operation(spec, "get", "/tasks/{task_id}")["parameters"]

    assert parameter["schema"]["type"] == "integer"
    assert parameter["schema"]["exclusiveMinimum"] == 0
    assert parameter["schema"]["maximum"] == 2_147_483_647


def test_openapi_request_bodies_reject_unknown_fields(spec):
    schemas = spec["components"]["schemas"]

    assert schemas["TaskCreate"]["additionalProperties"] is False
    assert schemas["TaskUpdate"]["additionalProperties"] is False
    assert schemas["TaskCreate"]["required"] == ["title"]


def test_the_task_schemas_expose_no_owner_field(spec):
    schemas = spec["components"]["schemas"]

    for name in ("TaskCreate", "TaskUpdate", "TaskRead"):
        assert not {"owner_id", "owner", "user_id"} & set(schemas[name]["properties"]), name


# --- POST /tasks ---------------------------------------------------------------------------------


def test_create_returns_201_with_the_stored_task(client, db):
    response = client.post("/tasks", json={"title": "  Write tests  "})

    assert response.status_code == 201
    body = response.json()
    assert body["id"] == 1
    assert body["title"] == "Write tests"  # trimmed by the schema
    assert body["description"] is None
    assert body["status"] == "todo"
    assert datetime.fromisoformat(body["created_at"]).tzinfo is not None
    assert set(body) == {"id", "title", "description", "status", "created_at"}
    # The user's row is locked and the user's tasks counted (the task limit) before the insert.
    assert writes(db) == ["scalar:User:for_update=True", "count:Task", "add:Task", "commit"]


def test_create_stores_the_supplied_fields(client):
    body = create(client, description="  Cover edge cases  ", status="in_progress")

    assert body["description"] == "Cover edge cases"
    assert body["status"] == "in_progress"


@pytest.mark.parametrize("blank", ["", "   "])
def test_create_normalizes_a_blank_description_to_null(client, blank):
    assert create(client, description=blank)["description"] is None


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"title": ""},
        {"title": "   "},
        {"title": "x" * 201},
        {"title": "bad\u0000title"},
        {"title": "ok", "description": "y" * 2001},
        {"title": "ok", "description": "bad\u0000text"},
        {"title": "ok", "status": "archived"},
        {"title": "ok", "id": 5},
        {"title": "ok", "created_at": "2026-01-01T00:00:00Z"},
        {"title": None},
    ],
)
def test_create_rejects_invalid_bodies_without_writing(client, db, payload):
    response = client.post("/tasks", json=payload)

    assert response.status_code == 422
    assert writes(db) == []


def post_raw(content, headers, method="POST", path="/tasks"):
    """Send a raw JSON body through a client that reports a crash as a 500 instead of raising."""
    lenient = TestClient(app, raise_server_exceptions=False, headers=headers)
    return lenient.request(method, path, content=content, headers={"content-type": "application/json"})


@pytest.mark.parametrize(
    "body",
    [
        '{"title": "lone \\ud800 surrogate"}',
        '{"title": "lone \\udfff low surrogate"}',
        '{"title": "ok", "description": "lone \\ud800 surrogate"}',
        '{"title": "ok", "\\ud800": 1}',  # in an unknown field name
        '{"title": ["\\ud800"]}',  # nested in another structure
    ],
)
def test_create_rejects_a_lone_surrogate_with_422_not_a_server_error(client, db, body):
    response = post_raw(body, client.headers)

    assert response.status_code == 422
    response.content.decode("utf-8")  # the body is fully encodable, so serialization did not crash
    assert response.json()["detail"]
    assert not any(0xD800 <= ord(char) <= 0xDFFF for char in response.text)
    assert "<unencodable text>" in response.text  # the rejected text is replaced, never echoed
    assert writes(db) == []


def test_patch_rejects_a_lone_surrogate_with_422(client, db):
    created = create(client)
    db.calls.clear()

    response = post_raw('{"title": "lone \\ud800 surrogate"}', client.headers, "PATCH", f"/tasks/{created['id']}")

    assert response.status_code == 422
    assert writes(db) == []


def test_the_error_for_a_lone_surrogate_keeps_the_usual_shape(client):
    [error] = post_raw('{"title": "lone \\ud800 surrogate"}', client.headers).json()["detail"]

    assert set(error) == {"type", "loc", "msg", "input"}
    assert error["loc"] == ["body", "title"]
    assert error["type"] == "string_unicode"
    assert error["input"] == "<unencodable text>"


def test_a_valid_surrogate_pair_is_just_an_emoji(client):
    response = client.post(
        "/tasks", content='{"title": "ok \\ud83d\\ude00"}', headers={"content-type": "application/json"}
    )

    assert response.status_code == 201
    assert response.json()["title"] == "ok \U0001f600"


def test_a_nul_character_is_still_echoed_normally(client):
    response = client.post("/tasks", json={"title": "bad\u0000title"})

    assert response.status_code == 422
    assert response.json()["detail"][0]["input"] == "bad\u0000title"  # encodable text is left untouched


ORDINARY_INVALID_REQUESTS = [
    ("POST", "/tasks", {"json": {}}),
    ("POST", "/tasks", {"json": {"title": ""}}),
    ("POST", "/tasks", {"json": {"title": "x" * 201}}),
    ("POST", "/tasks", {"json": {"title": "bad\u0000title"}}),
    ("POST", "/tasks", {"json": {"title": "ok", "status": "archived", "id": 5}}),
    ("POST", "/tasks", {"json": {"title": "café \U0001f600", "extra": "中文"}}),
    ("POST", "/tasks", {"content": "not json", "headers": {"content-type": "application/json"}}),
    ("PATCH", "/tasks/1", {"json": {}}),
    ("PATCH", "/tasks/1", {"json": {"title": None, "status": None}}),
    ("GET", "/tasks/abc", {}),
    ("GET", "/tasks/0", {}),
    ("DELETE", "/tasks/2147483648", {}),
]


@pytest.mark.parametrize("method, path, kwargs", ORDINARY_INVALID_REQUESTS)
def test_ordinary_validation_errors_are_identical_to_fastapis_default(db, client, method, path, kwargs):
    # A second app with the same routes but without our handler is the reference for the old behaviour.
    reference = FastAPI()
    reference.include_router(tasks.router)
    reference.dependency_overrides[get_db] = lambda: db

    ours = client.request(method, path, **kwargs)
    default = TestClient(reference, headers=client.headers).request(method, path, **kwargs)

    assert ours.status_code == default.status_code == 422
    assert ours.content == default.content
    assert ours.headers["content-type"] == default.headers["content-type"]


def test_without_unencodable_text_only_touches_unencodable_text():
    clean = {"loc": ("body", "title"), "input": ["café", {"k": "\U0001f600"}, 3, None, 1.5], "ok": True}

    assert without_unencodable_text(clean) == clean
    assert without_unencodable_text(clean)["loc"] == ("body", "title")  # tuples stay tuples
    assert without_unencodable_text({"\ud800": ["a\udfffb", ("x",)]}) == {UNENCODABLE_TEXT: [UNENCODABLE_TEXT, ("x",)]}
    assert without_unencodable_text(object) is object  # other types pass through untouched


def test_create_rejects_a_body_that_is_not_json(client, db):
    response = client.post("/tasks", content="not json", headers={"content-type": "application/json"})

    assert response.status_code == 422
    assert writes(db) == []


# --- GET /tasks ----------------------------------------------------------------------------------


def test_list_is_empty_when_there_are_no_tasks(client):
    response = client.get("/tasks")

    assert response.status_code == 200
    assert response.json() == []


def test_list_returns_tasks_in_ascending_id_order(client, db):
    for title in ("first", "second", "third"):
        create(client, title=title)

    response = client.get("/tasks")

    assert response.status_code == 200
    assert [task["id"] for task in response.json()] == [1, 2, 3]
    assert [task["title"] for task in response.json()] == ["first", "second", "third"]
    assert "ORDER BY tasks.id" in db.statement
    assert "DESC" not in db.statement  # ascending, and deterministic: id is unique


def test_list_only_reads(client, db):
    client.get("/tasks?limit=1&offset=5")

    assert writes(db) == ["count:Task", "scalars:Task"]


# --- GET /tasks/{id} -----------------------------------------------------------------------------


def test_get_returns_the_task(client, db):
    created = create(client, title="Find me")

    response = client.get(f"/tasks/{created['id']}")

    assert response.status_code == 200
    assert response.json() == created
    assert writes(db)[-1] == "scalar:Task:for_update=False"  # reads take no row lock


def test_get_returns_404_for_a_missing_task(client, db):
    response = client.get("/tasks/42")

    assert response.status_code == 404
    assert response.json() == NOT_FOUND
    assert writes(db) == ["scalar:Task:for_update=False"]  # nothing is written


# --- PATCH /tasks/{id} ---------------------------------------------------------------------------


def test_patch_changes_only_the_fields_that_were_sent(client, db):
    created = create(client, title="Original", description="Keep me", status="in_progress")

    response = client.patch(f"/tasks/{created['id']}", json={"title": "  Renamed  "})

    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "Renamed"
    assert body["description"] == "Keep me"  # omitted: untouched
    assert body["status"] == "in_progress"  # omitted: untouched
    assert body["id"] == created["id"] and body["created_at"] == created["created_at"]
    assert writes(db)[-2:] == ["scalar:Task:for_update=True", "commit"]  # row locked, then committed


def test_patch_with_null_description_clears_it(client):
    created = create(client, description="To be cleared")

    body = client.patch(f"/tasks/{created['id']}", json={"description": None}).json()

    assert body["description"] is None
    assert body["title"] == created["title"]


def test_patch_normalizes_the_description_like_create_does(client):
    created = create(client, description="old")

    trimmed = client.patch(f"/tasks/{created['id']}", json={"description": "  new  "}).json()
    blank = client.patch(f"/tasks/{created['id']}", json={"description": "   "}).json()

    assert trimmed["description"] == "new"
    assert blank["description"] is None


def test_patch_can_change_every_field_at_once(client):
    created = create(client)

    body = client.patch(
        f"/tasks/{created['id']}",
        json={"title": "New", "description": "Described", "status": "done"},
    ).json()

    assert (body["title"], body["description"], body["status"]) == ("New", "Described", "done")


def test_patch_with_unchanged_values_is_still_200(client):
    created = create(client)

    response = client.patch(f"/tasks/{created['id']}", json={"title": created["title"]})

    assert response.status_code == 200
    assert response.json() == created


def test_patch_returns_404_for_a_missing_task_and_writes_nothing(client, db):
    response = client.patch("/tasks/42", json={"title": "x"})

    assert response.status_code == 404
    assert response.json() == NOT_FOUND
    assert writes(db) == ["scalar:Task:for_update=True"]  # no commit


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"title": None},
        {"status": None},
        {"title": ""},
        {"title": "   "},
        {"title": "x" * 201},
        {"title": "bad\u0000title"},
        {"description": "y" * 2001},
        {"description": "bad\u0000text"},
        {"status": "archived"},
        {"id": 7},
        {"created_at": "2026-01-01T00:00:00Z"},
        {"title": "ok", "unknown": 1},
    ],
)
def test_patch_rejects_invalid_bodies_without_writing(client, db, payload):
    created = create(client)
    db.calls.clear()

    response = client.patch(f"/tasks/{created['id']}", json=payload)

    assert response.status_code == 422
    assert writes(db) == []


# --- DELETE /tasks/{id} --------------------------------------------------------------------------


def test_delete_returns_204_with_no_body_and_removes_the_task(client, db):
    created = create(client)

    response = client.delete(f"/tasks/{created['id']}")

    assert response.status_code == 204
    assert response.content == b""
    assert writes(db)[-3:] == ["scalar:Task:for_update=True", "delete", "commit"]
    assert client.get(f"/tasks/{created['id']}").status_code == 404
    assert client.get("/tasks").json() == []


def test_delete_returns_404_for_a_missing_task_and_deletes_nothing(client, db):
    response = client.delete("/tasks/42")

    assert response.status_code == 404
    assert response.json() == NOT_FOUND
    assert writes(db) == ["scalar:Task:for_update=True"]  # no delete, no commit


def test_deleting_twice_is_204_then_404(client):
    created = create(client)

    assert client.delete(f"/tasks/{created['id']}").status_code == 204
    assert client.delete(f"/tasks/{created['id']}").status_code == 404


def test_delete_leaves_other_tasks_alone(client):
    first, second = create(client, title="one"), create(client, title="two")

    client.delete(f"/tasks/{first['id']}")

    assert [task["id"] for task in client.get("/tasks").json()] == [second["id"]]


# --- Path id validation (shared by the id routes) ------------------------------------------------


def test_task_id_must_be_a_positive_integer(client, db):
    assert client.get("/tasks/0").status_code == 422
    assert client.get("/tasks/-1").status_code == 422
    assert client.get("/tasks/abc").status_code == 422
    assert writes(db) == []


def test_task_id_accepts_the_largest_postgres_integer(client):
    assert client.get("/tasks/2147483647").status_code == 404  # valid id, no such task


def test_task_id_above_the_postgres_integer_range_is_rejected(client, db):
    too_large = "2147483648"

    assert client.get(f"/tasks/{too_large}").status_code == 422
    assert client.patch(f"/tasks/{too_large}", json={"status": "done"}).status_code == 422
    assert client.delete(f"/tasks/{too_large}").status_code == 422
    assert writes(db) == []


def test_task_status_values_are_unchanged():
    assert [status.value for status in TaskStatus] == ["todo", "in_progress", "done"]
