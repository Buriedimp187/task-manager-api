from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_openapi_documents_task_routes():
    paths = client.get("/openapi.json").json()["paths"]

    assert set(paths["/tasks"]) == {"post", "get"}
    assert set(paths["/tasks/{task_id}"]) == {"get", "patch", "delete"}


def test_create_with_invalid_body_is_rejected_before_the_handler():
    response = client.post("/tasks", json={"title": ""})

    assert response.status_code == 422


def test_create_with_valid_body_reaches_not_implemented():
    response = client.post("/tasks", json={"title": "Write tests"})

    assert response.status_code == 501


def test_update_with_empty_body_is_rejected():
    response = client.patch("/tasks/1", json={})

    assert response.status_code == 422


def test_openapi_documents_not_implemented_response_on_every_route():
    spec = client.get("/openapi.json").json()
    task_operations = [
        operation
        for path, methods in spec["paths"].items()
        if path.startswith("/tasks")
        for operation in methods.values()
    ]

    assert len(task_operations) == 5
    for operation in task_operations:
        body = operation["responses"]["501"]["content"]["application/json"]["schema"]
        assert body == {"$ref": "#/components/schemas/NotImplementedResponse"}

    schema = spec["components"]["schemas"]["NotImplementedResponse"]
    assert schema["required"] == ["detail"]
    assert schema["properties"]["detail"]["type"] == "string"


def test_not_implemented_body_matches_documented_shape():
    response = client.get("/tasks")

    assert response.status_code == 501
    assert response.json() == {"detail": "Task persistence is not implemented yet"}


def test_task_id_must_be_a_positive_integer():
    assert client.get("/tasks/0").status_code == 422
    assert client.get("/tasks/-1").status_code == 422
    assert client.get("/tasks/abc").status_code == 422


def test_task_id_accepts_the_largest_postgres_integer():
    assert client.get("/tasks/2147483647").status_code == 501


def test_task_id_above_the_postgres_integer_range_is_rejected():
    too_large = "2147483648"

    assert client.get(f"/tasks/{too_large}").status_code == 422
    assert client.patch(f"/tasks/{too_large}", json={"status": "done"}).status_code == 422
    assert client.delete(f"/tasks/{too_large}").status_code == 422


def test_other_routes_are_not_implemented_yet():
    assert client.get("/tasks").status_code == 501
    assert client.get("/tasks/1").status_code == 501
    assert client.patch("/tasks/1", json={"status": "done"}).status_code == 501
    assert client.delete("/tasks/1").status_code == 501
