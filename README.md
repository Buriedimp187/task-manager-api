# Task Manager API

A REST API for managing tasks, built with Python, FastAPI, PostgreSQL, SQLAlchemy and Pytest.

Status: early development. The health endpoints work, and the database connection layer, the `Task` model and the first Alembic migration are in place. The `/tasks` routes and their request/response schemas are defined and validated, but they do not read or write the database yet and still return `501 Not Implemented`.

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

#### Migration integration tests

`pytest` runs the migration configuration tests without a database. The PostgreSQL integration tests are skipped unless you name a **separate, disposable** test database; they run migrations up and down on it, so never point them at a database that holds data you care about.

1. Create an empty database whose name ends in `_test`:

   ```bash
   docker compose exec db sh -c 'createdb -U "$POSTGRES_USER" taskmanager_test'
   ```

2. Run the tests with `TEST_POSTGRES_DB` set (PowerShell: `$env:TEST_POSTGRES_DB = "taskmanager_test"`; bash: `export TEST_POSTGRES_DB=taskmanager_test`):

   ```bash
   pytest tests/test_migrations.py
   ```

The tests refuse to run if the name does not end in `_test`, if it is the configured development database (including one named only in `.env`), if the database already contains tables other than `tasks` and `alembic_version`, or if its `tasks` table already holds rows. Only after those checks pass do the tests clear any leftover rows and drop the `tasks` table when they finish, so use a database that exists only for testing.

### API

```bash
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000/docs for the interactive documentation.

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

The default test run does not need a running database; the PostgreSQL migration tests are skipped unless `TEST_POSTGRES_DB` is set (see above).
