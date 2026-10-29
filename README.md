# Task Manager API

A REST API for managing tasks, built with Python, FastAPI, PostgreSQL, SQLAlchemy and Pytest.

Status: early development. The health endpoints work and the database connection layer is in place. The `/tasks` routes and their request/response schemas are defined and validated, but return `501 Not Implemented` until task models and persistence are added.

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

The current tests do not need a running database.
