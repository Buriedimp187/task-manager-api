# Task Manager API

A REST API for managing tasks, built with Python, FastAPI, PostgreSQL, SQLAlchemy and Pytest.

Status: early development. The task endpoints (create, list, get, update, delete) store their data in PostgreSQL, the schema is managed by Alembic migrations, and there are health endpoints. There is no authentication yet, so anyone who can reach the API can read and change every task.

## Run locally

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements-dev.txt
```

### Database

PostgreSQL runs in Docker (Docker Desktop must be running).

1. Copy `.env.example` to `.env` and replace every placeholder with your own values. `.env` is ignored by Git and must never be committed. If the password contains a `$`, wrap the value in single quotes, because Docker Compose would otherwise treat `$` as a variable and truncate the password.
2. Start the database:

   ```bash
   docker compose up -d --wait db
   ```

The database is published on `127.0.0.1` only, so it is not reachable from other machines.

#### Credentials and the data volume

Data is stored in the `postgres_data` Docker volume. PostgreSQL creates its user, password and database only the first time that volume is created. Changing `POSTGRES_USER`, `POSTGRES_PASSWORD` or `POSTGRES_DB` in `.env` afterwards does **not** update an existing database, and the API will fail to authenticate until `.env` matches the credentials the volume was created with.

- `docker compose down` stops the database and keeps your data.
- `docker compose down -v` also deletes the volume and **permanently destroys all data in it**. Use it only when you intentionally want to reset a disposable local database, for example to start over with new credentials. Never run it on a database whose contents you want to keep.

### Migrations

The database schema is managed only by [Alembic](https://alembic.sqlalchemy.org/) migrations; the application never creates tables on its own. Alembic reads the same `POSTGRES_*` settings as the API (from the environment or `.env`), so no credentials are stored in `alembic.ini`. Run these from the project root with the virtual environment active and the database running:

```bash
alembic upgrade head      # create or update the schema to the latest version
alembic current           # show which revision the database is at
alembic history           # list the available revisions
alembic upgrade head --sql   # print the SQL without touching the database
```

To apply the migrations to a fresh local development database, start it as described above and run `alembic upgrade head` once. Run it again after pulling new migrations.

After changing `app/models.py`, generate a migration, **read it before applying it**, and commit it together with the model change:

```bash
alembic revision --autogenerate -m "describe the change"
```

#### Downgrades can destroy data

`alembic downgrade -1` reverses the latest migration and `alembic downgrade base` reverses all of them. There is only one migration so far, so both do the same thing: they drop the `tasks` table. Dropping a table permanently deletes its rows, with no prompt and no undo.

To prevent accidents, the first migration's downgrade **refuses to run while the `tasks` table contains any rows**. It reports only how many there are, changes nothing, and leaves the database at the same revision. The downgrade first takes an exclusive lock on the table and holds it until it finishes, so a write from another session cannot slip in between the check and the drop: concurrent writers wait and then fail once the table is gone. The flip side is that the downgrade itself waits if another session has the table open in a long transaction. If you really mean to discard the tasks, delete them yourself first and then run the downgrade again. Open a `psql` session in the database container, run `DELETE FROM tasks;`, and leave with `\q`:

```bash
docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

```bash
alembic downgrade -1
```

Take a backup first (for example with `pg_dump`) if there is any chance you will want the data back. **The SQL printed by `alembic downgrade ... --sql` is not protected.** Offline mode cannot lock the table or look at its rows, so the script contains no check and drops the table unconditionally; running it by hand bypasses the guard. Read future migrations before downgrading, since they may drop columns or tables too.

| Database | Holds | Safe to wipe? |
|---|---|---|
| Development database (`POSTGRES_DB` in `.env`) | Your tasks | **No.** Never use it as `TEST_POSTGRES_DB`. |
| Test database (name ends in `_test`, created only for the tests) | Throwaway test rows | Yes. The tests clear it and drop `tasks`. |

#### PostgreSQL integration tests

`pytest` runs most tests without a database. The PostgreSQL integration tests (the migrations in `tests/test_migrations.py` and the task endpoints in `tests/test_tasks_postgres.py`) are skipped unless you name a **separate, disposable** test database. They run migrations up and down on it and store and delete tasks in it, so never point them at a database that holds data you care about.

1. Create an empty database whose name ends in `_test`:

   ```bash
   docker compose exec db sh -c 'createdb -U "$POSTGRES_USER" taskmanager_test'
   ```

2. Run the tests with `TEST_POSTGRES_DB` set (PowerShell: `$env:TEST_POSTGRES_DB = "taskmanager_test"`; bash: `export TEST_POSTGRES_DB=taskmanager_test`):

   ```bash
   pytest
   ```

The tests refuse to run if the name does not end in `_test`, if it is the configured development database (including one named only in `.env`), if the database already contains tables other than `tasks` and `alembic_version`, or if its `tasks` table already holds rows. Only after those checks pass do the tests clear any leftover rows and drop the `tasks` table when they finish, so use a database that exists only for testing.

### API

```bash
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000/docs for the interactive documentation.

## Tasks API

All request and response bodies are JSON.

| Request | Purpose | Success | Other responses |
|---|---|---|---|
| `POST /tasks` | Create a task | `201` with the task | `422` |
| `GET /tasks` | List every task, ordered by `id` ascending | `200` with an array (empty if there are none) | |
| `GET /tasks/{id}` | Fetch one task | `200` with the task | `404`, `422` |
| `PATCH /tasks/{id}` | Change some fields of a task | `200` with the updated task | `404`, `422` |
| `DELETE /tasks/{id}` | Delete a task | `204` with no body | `404`, `422` |

Any request can also return `503` or `500`, described under "Errors" below.

**Fields**

| Field | Rules |
|---|---|
| `id` | Read-only integer from 1 to 2,147,483,647, assigned by the database. |
| `title` | Required. Surrounding whitespace is trimmed; 1 to 200 characters. |
| `description` | Optional. Trimmed, up to 2,000 characters; a blank value is stored as `null`. |
| `status` | `todo` (the default), `in_progress` or `done`. |
| `created_at` | Read-only, timezone-aware, set by the database. |

Unknown fields are rejected, so `id` and `created_at` cannot be set by a client. The NUL character (`\u0000`) is rejected in `title` and `description`, because PostgreSQL cannot store it.

```http
POST /tasks
{"title": "  Write the README  ", "description": "Cover every endpoint"}

201 Created
{"id": 1, "title": "Write the README", "description": "Cover every endpoint",
 "status": "todo", "created_at": "2026-10-28T23:59:59.123456Z"}
```

**Updating.** `PATCH` changes only the fields you send. A field you leave out is not touched, `"description": null` clears the description, and `null` is not allowed for `title` or `status`. An empty body is rejected with `422`. Updating a task to the values it already has still returns `200`.

```http
PATCH /tasks/1
{"status": "done"}

200 OK
{"id": 1, "title": "Write the README", "description": "Cover every endpoint",
 "status": "done", "created_at": "2026-10-28T23:59:59.123456Z"}
```

**Concurrent changes.** `PATCH` and `DELETE` lock the task's row until they finish, so two changes to the same task are applied one after the other (the last one wins; there is no versioning). A `PATCH` that loses a race with a `DELETE` gets `404`.

**Errors**

| Status | Body | Meaning |
|---|---|---|
| `404` | `{"detail": "Task not found"}` | No task has that id. This includes `DELETE`. |
| `422` | FastAPI's standard validation details, which echo the rejected value in `input` | Invalid body or id. Nothing was changed. Text that cannot be encoded as UTF-8, such as a lone surrogate (`"\ud800"` in JSON), is shown as `<unencodable text>` instead of being echoed. |
| `503` | `{"detail": "Database unavailable"}` | The database could not be reached. This includes a wrong password, user or database name in the settings, which PostgreSQL's driver reports before the server can answer, so they cannot be told apart from an outage. |
| `500` | `Internal Server Error` | Unexpected errors and missing or invalid configuration. The response never contains details or credentials. |

Database errors are never returned to the client. For a `503` the server log records only the exception class names.

**Limitations.** `GET /tasks` returns every task; there is no pagination, filtering or search. Tasks have no `updated_at`.

## Health checks

| Endpoint | Meaning | Touches the database |
|---|---|---|
| `GET /health` | Liveness: the API process is running. | No |
| `GET /health/db` | Readiness: the API can run `SELECT 1` against PostgreSQL. Returns `200`, or `503` with a generic body if the database is unavailable. | Yes |

If `/health` returns `200` but `/health/db` returns `503`, the API is up and the database is not reachable. If the database settings are missing or invalid, `/health/db` returns `500` instead, so a configuration error can be told apart from a database outage.

## Test

```bash
pytest
```

The default test run does not need a running database. The task endpoint tests in that run use an in-memory fake session, which checks the API contract but proves nothing about PostgreSQL; persistence, ordering, locking and constraints are covered by the PostgreSQL integration tests, which are skipped unless `TEST_POSTGRES_DB` is set (see above).
