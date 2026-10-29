# Task Manager API

A REST API for managing personal task lists, built with Python, FastAPI, PostgreSQL, SQLAlchemy, Alembic and Pytest.
People register, log in, and manage their own tasks; nobody else can see or change them.

**Status: early development, no releases yet.** The code has not had an independent security review or audit. The
GitHub Actions workflow's first run on GitHub, a push to the then-private repository, passed all of its jobs; the
workflow has not yet run on a public repository. See
[Known limitations](#known-limitations-and-operational-notes) before relying on it for anything important.

## Features

- Accounts with registration and login; passwords stored as Argon2id hashes; short-lived (30 minute) signed access tokens.
- Tasks (create, list, get, update, delete) that belong to one account. Another account's task looks exactly like a
  missing one (`404`).
- Paginated task listing (default 50, at most 200 per page), and a limit of 1,000 tasks per account.
- Rate limiting of login and registration, a 64 KiB request-body limit, and registration that can be switched off.
- PostgreSQL with Alembic migrations (downgrades refuse to destroy data), statement, lock and idle-transaction
  timeouts, a bounded connection pool, and a controlled `503` when the database is unavailable or busy.
- Liveness and readiness health endpoints.
- A hardened container image and a production Docker Compose stack, separate from the development workflow.
- Ruff, a test suite with PostgreSQL integration tests, and a GitHub Actions workflow (its first run on GitHub passed).

## Contents

[Requirements](#requirements) · [Run locally](#run-locally) · [Configuration](#configuration-reference) ·
[Docker](#docker-development-and-production) · [Authentication](#authentication) · [Tasks API](#tasks-api) ·
[Limits at a glance](#limits-at-a-glance) · [Health checks](#health-checks) · [Test](#test) ·
[Continuous integration](#continuous-integration) · [Known limitations](#known-limitations-and-operational-notes) ·
[Security](#security) · [License](#license)

## Requirements

- Python **3.13 or 3.14**. The CI workflow targets both, the Docker image uses 3.13, and development has been done on 3.14.
- Docker with Compose v2, to run PostgreSQL (and the production stack). Docker Desktop must be running on Windows and macOS.
- Git.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate         # Linux / macOS
# .venv\Scripts\Activate.ps1      # Windows PowerShell
# .venv\Scripts\activate.bat      # Windows cmd
pip install -r requirements-dev.txt -c constraints.txt
```

`constraints.txt` fixes every dependency (including transitive ones) to the versions the project was tested with.

Then, in short: copy `.env.example` to `.env` and fill it in, start the database, create the schema, and run the API.
Each step is explained below.

```bash
docker compose up -d --wait db
alembic upgrade head
uvicorn app.main:app --reload --no-proxy-headers
```

### Database

PostgreSQL runs in Docker (Docker Desktop must be running).

1. Copy `.env.example` to `.env` and replace every placeholder with your own values. `.env` is ignored by Git and must never be committed. If the password contains a `$`, wrap the value in single quotes, because Docker Compose would otherwise treat `$` as a variable and truncate the password.
2. Start the database:

   ```bash
   docker compose up -d --wait db
   ```

In development the database is published on `127.0.0.1` only (by `docker-compose.override.yml`), so it is not reachable from
other machines. The production stack does not publish it at all (see "Docker: development and production").

#### Credentials and the data volume

Data is stored in the `postgres_data` Docker volume (its full name is `task-manager-api_postgres_data`; if you have an
older one, see "Volume names changed" below). PostgreSQL creates its user, password and database only the first time that volume is created. Changing `POSTGRES_USER`, `POSTGRES_PASSWORD` or `POSTGRES_DB` in `.env` afterwards does **not** update an existing database, and the API will fail to authenticate until `.env` matches the credentials the volume was created with.

- `docker compose down` stops the database and keeps your data.
- `docker compose down -v` also deletes the volume and **permanently destroys all data in it**. Use it only when you intentionally want to reset a disposable local database, for example to start over with new credentials. Never run it on a database whose contents you want to keep.

### Signing key

The API signs login tokens with `SECRET_KEY` from `.env`. It must be at least 32 characters, and the API **refuses to start** with a clear message if it is missing or too short (the value is never printed). The placeholder in `.env.example` is deliberately too short to work. Generate a real one with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Keep it secret and out of Git: anyone who knows it can forge a login for any account. Changing it invalidates every token that was issued, so everyone has to log in again. Alembic does not need it, so migrations run without a signing key.

### Configuration reference

Settings come from environment variables, and from `.env` for anything not set in the environment (the environment wins).
`.env.example` lists all of them with placeholders; copy it to `.env`, which Git ignores. **Never commit `.env` or put a real
password or `SECRET_KEY` in any tracked file.** Invalid values are rejected with a validation error that does not echo
secrets.

| Variable | Default | Purpose and details |
|---|---|---|
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | none, **required** | The database account and name. Compose creates the database with these the first time. |
| `POSTGRES_HOST`, `POSTGRES_PORT` | `127.0.0.1`, `5432` | Where the API connects when it runs on your machine. In the container stack the host is always `db`. `POSTGRES_PORT` is also the host port the development database is published on. |
| `SECRET_KEY` | none, **required** | Signs access tokens; at least 32 characters. See "Signing key". |
| `DB_*`, `REQUEST_THREADS` | see "Database limits" | Timeouts, connection pool and worker threads. |
| `LOGIN_*`, `REGISTER_*`, `TRUSTED_PROXIES` | see "Abuse protection" | Rate limits and trusted reverse proxies. |
| `REGISTRATION_ENABLED` | `true` (`false` in the production Compose file) | Whether `POST /auth/register` works. |
| `DOCS_ENABLED` | `true` (`false` in the production Compose file) | Whether `/docs`, `/redoc` and `/openapi.json` exist. |
| `MAX_TASKS_PER_USER` | `1000` | See "Tasks API". |
| `API_PORT` | `8000` | Docker Compose only: the host port the containerized API is published on (on `127.0.0.1`). |
| `TEST_POSTGRES_DB` | unset | Tests only: a disposable database for the PostgreSQL integration tests. See "PostgreSQL integration tests". |
| `COMPOSE_PROJECT_NAME` | `task-manager-api` | Docker Compose only; see "Volume names changed". |

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

There are two migrations: `0001` creates `tasks`, and `0002` adds `users` and the `tasks.owner_id` column. `alembic downgrade -1` reverses the latest one (dropping `users` and `owner_id`) and `alembic downgrade base` reverses both. Dropping a table permanently deletes its rows, with no prompt and no undo.

To prevent accidents, each downgrade **refuses to run while it would destroy data**, reports only how many rows are involved, changes nothing, and leaves the database at the same revision:

- `0002`'s downgrade refuses while **any account exists** (dropping `users` would destroy accounts and password hashes, and every task needs an owner).
- `0001`'s downgrade refuses while **any task exists**.

Each locks its tables exclusively until it finishes, so a write from another session cannot slip in between the check and the drop: concurrent writers wait and then fail once the table is gone. The flip side is that the downgrade itself waits if another session has the table open in a long transaction. If you really mean to discard the data, delete it yourself first and then run the downgrade again. Open a `psql` session in the database container, run `DELETE FROM tasks; DELETE FROM users;`, and leave with `\q`:

```bash
docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

```bash
alembic downgrade -1
```

Take a backup first (for example with `pg_dump`) if there is any chance you will want the data back. **The SQL printed by `alembic downgrade ... --sql` is not protected.** Offline mode cannot lock the tables or look at their rows, so the script contains no check and drops them unconditionally; running it by hand bypasses the guard. Read future migrations before downgrading, since they may drop columns or tables too.

#### Upgrading a database that already has tasks

Tasks created before accounts existed have no owner, and an owner cannot be invented: guessing one would hand someone's data to the wrong account. So `alembic upgrade head` **refuses** when the `tasks` table contains any rows. It reports how many, changes nothing (the whole upgrade is rolled back) and tells you what to do: export or delete those tasks (`DELETE FROM tasks;` in a `psql` session, as above), then run the upgrade again. It never assigns owners and never discards data on its own. A database with no tasks upgrades normally. (The SQL from `alembic upgrade head --sql` cannot check the data, but it is safe on this point: PostgreSQL itself rejects adding the required `owner_id` column to a table that has rows.)

| Database | Holds | Safe to wipe? |
|---|---|---|
| Development database (`POSTGRES_DB` in `.env`) | Your accounts and tasks | **No.** Never use it as `TEST_POSTGRES_DB`. |
| Test database (name ends in `_test`, created only for the tests) | Throwaway test rows | Yes. The tests clear it and drop `tasks` and `users`. |

#### PostgreSQL integration tests

`pytest` runs most tests without a database. The PostgreSQL integration tests (the migrations in `tests/test_migrations.py`, the task endpoints in `tests/test_tasks_postgres.py` and registration and login in `tests/test_auth_postgres.py`) are skipped unless you name a **separate, disposable** test database. They run migrations up and down on it and create and delete accounts and tasks in it, so never point them at a database that holds data you care about.

1. Create an empty database whose name ends in `_test`:

   ```bash
   docker compose exec db sh -c 'createdb -U "$POSTGRES_USER" taskmanager_test'
   ```

2. Run the tests with `TEST_POSTGRES_DB` set (PowerShell: `$env:TEST_POSTGRES_DB = "taskmanager_test"`; bash: `export TEST_POSTGRES_DB=taskmanager_test`):

   ```bash
   pytest
   ```

The tests refuse to run if the name does not end in `_test`, if it is the configured development database (including one named only in `.env`), if the database already contains tables other than `tasks`, `users` and `alembic_version`, or if it already holds any task or any account. Only after those checks pass do the tests clear any leftover rows and drop the tables when they finish, so use a database that exists only for testing.

### Database limits

The API protects itself and the database from slow or stuck work. Every setting is optional and can be set in `.env` (see `.env.example`):

| Setting | Default | Meaning |
| --- | --- | --- |
| `DB_STATEMENT_TIMEOUT_MS` | 5000 | A single statement running longer is cancelled. |
| `DB_LOCK_TIMEOUT_MS` | 3000 | Waiting longer than this for a row lock fails. |
| `DB_IDLE_IN_TRANSACTION_TIMEOUT_MS` | 15000 | A transaction left idle this long is closed by the server. |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | 10 / 10 | Connections kept open / extra connections allowed under load. |
| `DB_POOL_TIMEOUT_SECONDS` | 5 | How long a request waits for a free connection slot before getting a 503. |
| `DB_POOL_RECYCLE_SECONDS` | 1800 | Connections older than this are replaced. |
| `DB_CONNECT_TIMEOUT_SECONDS` | 5 | How long opening a new connection may take. |
| `REQUEST_THREADS` | 20 | Worker threads for requests; keep it at or below pool size + overflow. |

Zero, negative and out-of-range values are rejected with a clear validation error (`REQUEST_THREADS` when the server starts, the `DB_*` values when the database is first used). When the database is unreachable the API answers `503 {"detail": "Database unavailable"}`. When it is up but busy (a lock or statement timeout, a closed idle transaction, or no free connection in time) it answers `503 {"detail": "Database busy, retry shortly"}` with `Retry-After: 1`; the request can safely be retried. At most pool size + overflow requests use the database at the same time; further requests wait for a slot (up to `DB_POOL_TIMEOUT_SECONDS`) without occupying a worker thread. Migrations are not subject to these limits. On shutdown the connection pool is closed cleanly.

### API

With `.env` filled in (database settings and `SECRET_KEY`) and the database running:

```bash
alembic upgrade head
uvicorn app.main:app --reload --no-proxy-headers
```

Open http://127.0.0.1:8000/docs for the interactive documentation (it exists while `DOCS_ENABLED` is true, which is the default
outside the production Compose file). Register and log in with the `/auth` endpoints, then use **Authorize** and paste the
`access_token`.

## Docker: development and production

There are two ways to use Docker Compose with this project. The files make the difference explicit:

| | Development (default) | Production |
|---|---|---|
| Command | `docker compose up -d --wait db` | `docker compose -f docker-compose.yml up -d --build --wait` |
| Files | `docker-compose.yml` + `docker-compose.override.yml`, loaded automatically | `docker-compose.yml` only (naming it with `-f` skips the override) |
| Starts | PostgreSQL only; you run the API yourself with `uvicorn --reload` | PostgreSQL, a one-shot migration, then the API |
| PostgreSQL port | published on `127.0.0.1:${POSTGRES_PORT:-5432}` | **not published**: only the stack's own containers reach it |
| API | on your machine | in a container, published on `127.0.0.1:${API_PORT:-8000}` |
| Registration, `/docs` | on | **off** unless `REGISTRATION_ENABLED=true` / `DOCS_ENABLED=true` is set in `.env` |

To try the containerized API during development, add the `app` profile: `docker compose --profile app up -d --build --wait` (registration and `/docs` stay on there).

**What the production stack does**

- The image (`Dockerfile`) is built in two stages from a digest-pinned `python:3.13-slim`. Only the runtime requirements are installed, with every version fixed by `constraints.txt`, and the final image has no compiler, no `pip`, no tests and no `.env`. The application runs as an unprivileged user (UID 10001) and cannot modify the code.
- The API and migration containers run with a read-only root filesystem, no Linux capabilities, `no-new-privileges` and a process limit; the API gets a small `/tmp` in memory. The database container drops all capabilities except the five its entrypoint needs.
- Start-up order is database (healthy), then `alembic upgrade head`, then the API. Migrations never run downgrades. If one fails, the API does not start.
- The API health check calls `/health/db`, so the container is `unhealthy` while the database is unreachable (about 45 seconds into an outage) and recovers by itself when it returns; it is not restarted for that.
- `docker stop` sends SIGTERM to uvicorn, which finishes in-flight requests (up to 15 seconds), closes the connection pool and exits with code 0.
- uvicorn runs with `--no-proxy-headers`, so `X-Forwarded-For` is ignored. Behind a reverse proxy, set `TRUSTED_PROXIES` to the proxy's address; see "Abuse protection".
- Secrets are never in the image or in the Compose files. `POSTGRES_*` and `SECRET_KEY` must be set in `.env` (or the environment), or Compose refuses to start. Every variable in `.env` is passed to the API container, not only the settings it reads (see "Known limitations").

**Operating it**

- Put a TLS-terminating reverse proxy in front of the loopback port; the API itself speaks plain HTTP.
- Run **one API container**. The rate limiter and the connection limiter keep their state in memory (see "Abuse protection").
- Data lives in the `postgres_data` volume of the `task-manager-api` project. Back it up (for example with `pg_dump`) before upgrading; `docker compose down` keeps it and `down -v` destroys it.
- **Volume names changed.** The database volume is `<project name>_postgres_data`. This stack now has the fixed project name `task-manager-api`, so its volume is `task-manager-api_postgres_data`. Earlier versions had no fixed name, so Compose used the folder name lowercased with spaces removed (for a folder called `My Project`: `myproject_postgres_data`). Nothing deletes or replaces an old volume, but a stack started now creates a **new, empty** database next to it, and the API will appear to have lost its data. Before upgrading, run `docker volume ls --filter name=_postgres_data` to see whether you have an old volume. To keep using it, put its project name (the part before `_postgres_data`) in `.env` as `COMPOSE_PROJECT_NAME=<old name>`, or pass `-p <old name>` to every `docker compose` command; either overrides the fixed name. To move to the new name instead, dump the old database (`pg_dump`) and restore it into the new one.
- Update the pinned image digests in `Dockerfile` and `docker-compose.yml` on purpose when taking base-image security updates.

## Authentication

Anyone can create an account, then log in to get an access token. The health endpoints are public; everything under `/tasks` needs a token.

| Request | Purpose | Success | Other responses |
|---|---|---|---|
| `POST /auth/register` | Create an account | `201` with `id`, `username`, `created_at` | `403` (registration disabled), `409` (name taken), `422`, `429` |
| `POST /auth/login` | Trade a username and password for an access token | `200` with the token | `401`, `422`, `429` |

```http
POST /auth/register
{"username": "alice", "password": "a long passphrase"}

201 Created
{"id": 1, "username": "alice", "created_at": "2026-10-29T00:00:00.123456Z"}

POST /auth/login
{"username": "alice", "password": "a long passphrase"}

200 OK
{"access_token": "<token>", "token_type": "bearer", "expires_in": 1800}
```

Send the token on every task request as `Authorization: Bearer <token>`. A token lasts **30 minutes**; there are no refresh tokens, so log in again when it expires.

### Abuse protection

Login and registration are rate limited per client address, before any password is hashed or the database is touched. Every limit is configurable (see `.env.example`):

| Setting | Default | Meaning |
| --- | --- | --- |
| `LOGIN_IP_LIMIT` / `LOGIN_IP_WINDOW_SECONDS` | 10 / 60 | Login attempts per address (all usernames). |
| `LOGIN_USER_LIMIT` / `LOGIN_USER_WINDOW_SECONDS` | 5 / 60 | Attempts per address and username (case-insensitive). A successful login clears it. |
| `REGISTER_IP_LIMIT` / `REGISTER_IP_WINDOW_SECONDS` | 5 / 3600 | Registration attempts per address, successful or not. |
| `REGISTRATION_ENABLED` | `true` (`false` in the production Compose file) | `false` makes `POST /auth/register` answer `403`; login is unaffected. |
| `TRUSTED_PROXIES` | empty | Comma-separated addresses or networks of reverse proxies whose `X-Forwarded-For` header is believed. |

Over the limit the API answers `429 {"detail": "Too many requests. Try again later."}` with a `Retry-After` header. The response is identical whichever limit was hit and whether or not the account exists, and a correct password is refused too while limited. IPv6 clients are grouped by /64.

**Limitations and deployment.**

- Counters live in this process's memory: they reset on restart, and with several processes or replicas each counts separately, so the real limit is multiplied. Run one process, or add a shared store.
- Clients are identified by their connection address. `X-Forwarded-For` is ignored unless the connection comes from a `TRUSTED_PROXIES` entry; then the nearest address in the header that is not itself a trusted proxy is used.
- **Start uvicorn with `--no-proxy-headers`** (the Docker image already does). By default uvicorn rewrites the client address from `X-Forwarded-For` for connections from `127.0.0.1`, which lets a local client evade the limits (this was tested).
- Behind a reverse proxy, list the proxy in `TRUSTED_PROXIES` (and keep `--no-proxy-headers`). Without it every user appears to come from the proxy's address and shares one set of limits. The proxy must set or append `X-Forwarded-For` itself, not pass a client's value through unchanged. Do not set uvicorn's `FORWARDED_ALLOW_IPS` or pass `--proxy-headers`: they make uvicorn trust the header independently of this setting.
- Many clients behind one address (a company NAT) share its limits.
- Failed registrations count against the registration limit too, and a registration attempt for a taken name reveals that the
  name exists (see the table below).

**Usernames** are 3 to 32 characters: ASCII letters, digits, `_`, `-` and `.`. They are unique **ignoring case**, enforced by PostgreSQL itself (`Alice` and `alice` cannot both exist, even if two registrations arrive at the same moment). The case you chose is kept, and logging in ignores case. **Passwords** are 10 to 128 characters and are used exactly as typed (not trimmed). They are stored only as an Argon2id hash (19 MiB, 2 passes, 1 lane, the minimum OWASP recommends) and are never returned or logged.

**What a client sees**

| Situation | Response |
|---|---|
| Wrong password, or a username that does not exist | `401 {"detail": "Incorrect username or password"}`, identical in both cases (and a comparable amount of work is done, so timing does not tell them apart) |
| Missing, malformed, expired, wrongly signed or otherwise invalid token, or an account that no longer exists | `401 {"detail": "Not authenticated"}` with `WWW-Authenticate: Bearer`. The reason is never given. |
| Registering a name that is taken, in any letter case | `409 {"detail": "Username already taken"}` (registration necessarily reveals this) |
| Invalid registration or login body | `422` **without the `input` field**, so a submitted password is never echoed back, not even inside an error about a missing field |

Tokens are signed with HS256. The server fixes the accepted algorithm and requires the `sub`, `iat` and `exp` claims, so unsigned tokens, tokens signed with another key or algorithm, and tokens missing a claim are all refused. The claims are checked strictly: a token must carry exactly `sub`, `iat` and `exp`; `iat` and `exp` must be whole numbers (not strings, decimals or booleans), `exp` must be in the future and after `iat`, `iat` may not be in the future (beyond 5 seconds of clock skew), the token may not live longer than the 30 minutes this server issues, and `sub` must be a positive integer that fits the user id column. Every failure is the same `401`. The password hash used to keep the login time of unknown usernames in line with real ones is created once when the server starts.

**Limitations.** Registration is open to anyone unless `REGISTRATION_ENABLED` is `false`. There is no password change or reset,
email verification, account deletion or roles. A token cannot be revoked before it expires (logging out is up to the client).
Rate limiting is per client address and per process (see "Abuse protection"), so a distributed attack from many addresses is
limited only by each address's own budget.

## Tasks API

All request and response bodies are JSON. **Every request needs a token** (see Authentication). Each task belongs to the account that created it, and you only ever see and change your own.

| Request | Purpose | Success | Other responses |
|---|---|---|---|
| `POST /tasks` | Create a task | `201` with the task | `401`, `409` (task limit reached), `422` |
| `GET /tasks?limit=&offset=` | List one page of your tasks, ordered by `id` ascending | `200` with an array (empty if there are none) and an `X-Total-Count` header | `401`, `422` |
| `GET /tasks/{id}` | Fetch one of your tasks | `200` with the task | `401`, `404`, `422` |
| `PATCH /tasks/{id}` | Change some fields of one of your tasks | `200` with the updated task | `401`, `404`, `422` |
| `DELETE /tasks/{id}` | Delete one of your tasks | `204` with no body | `401`, `404`, `422` |

Any request can also return `503` or `500`, described under "Errors" below.

**Pagination.** `GET /tasks` returns at most `limit` tasks (default **50**, 1 to **200**), skipping `offset` (default 0, not negative) of your tasks in ascending `id` order. The body is still a plain array. The `X-Total-Count` header holds how many tasks you have in total, whatever the page, so a client can compute the number of pages. Because the order is by the unique `id`, walking pages with a growing `offset` returns each task once as long as nothing is added or deleted in between; the total and the page are read in one request but not as one snapshot. **This is a contract change**: earlier versions returned every task, so a client that never sent `limit` now receives at most 50.

**Task limit.** An account can hold at most **1,000** tasks (`MAX_TASKS_PER_USER`, 1 to 1,000,000). Creating one more is refused with `409 {"detail": "Task limit of 1000 reached"}`; deleting a task frees a slot. Reading, updating and deleting are never refused. The limit is enforced by locking the account's own row while a task is created, so simultaneous requests from one account take turns and cannot exceed it, while other accounts are not delayed.

**Fields**

| Field | Rules |
|---|---|
| `id` | Read-only integer from 1 to 2,147,483,647, assigned by the database. |
| `title` | Required. Surrounding whitespace is trimmed; 1 to 200 characters. |
| `description` | Optional. Trimmed, up to 2,000 characters; a blank value is stored as `null`. |
| `status` | `todo` (the default), `in_progress` or `done`. |
| `created_at` | Read-only, timezone-aware, set by the database. |

Unknown fields are rejected, so `id` and `created_at` cannot be set by a client. The NUL character (`\u0000`) is rejected in `title` and `description`, because PostgreSQL cannot store it.

**Ownership.** The owner is taken from your token and nowhere else. There is no owner field: sending `owner_id`, `user_id` or any other unknown field is rejected with `422`, and a query string or header cannot choose an owner either. A task that belongs to someone else is reported as **`404`, exactly like a task that does not exist** (same status, body and headers), so ids cannot be probed. A request for someone else's task also never waits on that task's lock.

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
| `401` | `{"detail": "Not authenticated"}` | No valid token. Checked before the body or the id, so an unauthenticated request never learns whether a task exists. |
| `404` | `{"detail": "Task not found"}` | None of **your** tasks has that id: it does not exist, or it belongs to someone else. This includes `DELETE`. |
| `409` | `{"detail": "Task limit of 1000 reached"}` | `POST /tasks` only: the account already holds the maximum number of tasks. |
| `413` | `{"detail": "Request body too large"}` | Any request whose body is over 64 KiB (see "Limits at a glance"). A malformed `Content-Length` header gets `400 {"detail": "Invalid Content-Length header"}`. |
| `422` | FastAPI's standard validation details, which echo the rejected value in `input` (except under `/auth`, see above) | Invalid body or id. Nothing was changed. Text that cannot be encoded as UTF-8, such as a lone surrogate (`"\ud800"` in JSON), is shown as `<unencodable text>` instead of being echoed. |
| `503` | `{"detail": "Database unavailable"}` | The database could not be reached. This includes a wrong password, user or database name in the settings, which PostgreSQL's driver reports before the server can answer, so they cannot be told apart from an outage. |
| `503` | `{"detail": "Database busy, retry shortly"}` with `Retry-After: 1` | The database is up but a lock or statement timed out, or no connection was free in time. Retry. A task creation that waits more than the lock timeout (3 s by default) for another creation by the same account ends this way. |
| `429` | `{"detail": "Too many requests. Try again later."}` with `Retry-After` | `/auth/register` and `/auth/login` only; see "Abuse protection". |
| `500` | `Internal Server Error` | Unexpected errors and missing or invalid configuration. The response never contains details or credentials. |

Database errors are never returned to the client. For a `503` the server log records only the exception class names.

**Limitations.** There is no filtering or search, and `GET /tasks` is paginated only by `limit` and `offset`. Tasks have no `updated_at`.

## Limits at a glance

| Limit | Value | When exceeded |
|---|---|---|
| Request body | 64 KiB (65,536 bytes), counted as it arrives as well as from `Content-Length`, so chunked uploads are covered | `413`. A client still uploading when the limit is reached may see its connection reset before it can read the `413`. |
| Page size | 50 by default, 200 at most | `422` for `limit` above 200 or below 1 |
| Tasks per account | 1,000 (`MAX_TASKS_PER_USER`) | `409` on creation |
| Login attempts | 10 per minute per address, 5 per minute per address and username | `429` |
| Registration attempts | 5 per hour per address | `429` |
| Access token lifetime | 30 minutes | `401` after expiry |
| Statement, lock and idle-transaction time | 5 s, 3 s and 15 s | `503` (busy) |
| Concurrent database use | pool size + overflow (20) requests at once; the rest wait up to 5 s | `503` (busy) |

The request body limit, the token lifetime and the page sizes (50 and 200) are fixed in the code. The others are settings; see "Configuration reference".

## Health checks

| Endpoint | Meaning | Touches the database |
|---|---|---|
| `GET /health` | Liveness: the API process is running. | No |
| `GET /health/db` | Readiness: the API can run `SELECT 1` against PostgreSQL. Returns `200`, or `503` with a generic body if the database is unavailable. | Yes |

If `/health` returns `200` but `/health/db` returns `503`, the API is up and the database is not reachable. If the database settings are missing or invalid, `/health/db` returns `500` instead, so a configuration error can be told apart from a database outage.

## Test

```bash
pytest                  # the whole suite; the PostgreSQL integration tests skip themselves without a test database
ruff check .            # lint
ruff format --check .   # formatting
```

The default test run does not need a running database. The task endpoint tests in that run use an in-memory fake session, which checks the API contract but proves nothing about PostgreSQL; persistence, ordering, locking and constraints are covered by the PostgreSQL integration tests, which are skipped unless `TEST_POSTGRES_DB` is set (see above).

## Continuous integration

> **First run on GitHub passed; no run on a public repository yet.** The workflow ran on GitHub for the first time on
> 2026-10-29 (run 40842390327, a push of `main` at commit `082d72a`, while the repository was still private). All four job
> runs succeeded: the lint and test job on Python 3.13 and on 3.14, the PostgreSQL job, and the Docker image and Compose stack
> job. The whole run took under two minutes. What has not been exercised is anything that only happens on a public
> repository, such as workflows triggered by pull requests from forks and the approval settings that apply to them.

`.github/workflows/ci.yml` runs on every pull request, on every push to the repository's **default branch**, and on demand (**Actions** > **CI** > **Run workflow**). No branch name is written in the file: each job checks `github.event.repository.default_branch`, so it keeps working if the default branch is not called `main`. A push to another branch starts the workflow with every job skipped, and its pull request is tested once. A newer push to a pull request cancels the run in progress; runs on the default branch are never cancelled. It needs no secrets, has read-only access to the repository, and deploys or publishes nothing.

| Job | What it checks |
|---|---|
| Lint and tests (Python 3.13 and 3.14) | Dependencies install from `requirements-dev.txt` with `constraints.txt` and `pip check` passes; `ruff check`; `ruff format --check`; the whole suite **without** a database. The tests that need PostgreSQL skip themselves, and the job fails if anything else is skipped. |
| Tests with PostgreSQL (Python 3.13) | The whole suite against a throwaway PostgreSQL 17 service container: it waits until the database accepts connections, creates a separate `ci_test` database for the destructive tests, and fails if **any** test is skipped. |
| Docker image and Compose stack | Both Compose files validate; the image builds and runs as a non-root user; the production stack starts (database, migration, API) and answers `/health`, `/health/db`, a `404` on `/docs` and a `403` on registration. |

The Python versions are the one in the Docker image (3.13) and the one used for development (3.14). Both have been tested on
Linux, locally and on GitHub: the workflow's own commands, including the PostgreSQL job, were first run with the pinned
dependencies in `python:3.13-slim` and `python:3.14-slim` containers, and the GitHub run above repeated them on GitHub-hosted
`ubuntu-24.04` runners with Python 3.13.17 and 3.14.9 (782 tests passed and 102 skipped in each lint-and-test job, and 884
passed in the PostgreSQL job). The same pinned versions are used everywhere: ruff, pytest and every transitive dependency
come from `requirements-dev.txt` and `constraints.txt`, so CI and your machine run the same tools. Third-party actions are pinned
to full commit SHAs (the version is in the comment beside each), and the database image digests are pinned too. Update all of
them on purpose.

The workflow does not scan dependencies or images for known vulnerabilities, and nothing updates the pinned versions
automatically.

**Running the same checks locally** (PowerShell or bash, with the virtual environment active):

```bash
pip install -r requirements-dev.txt -c constraints.txt
ruff check .
ruff format --check .
pytest                          # no database: the PostgreSQL tests skip
```

For the PostgreSQL job, start a disposable database (see "PostgreSQL integration tests" above), create a database whose name ends in `_test`, and run `pytest` with `TEST_POSTGRES_DB` set to it. CI uses a different database name for the settings (`ci_db`) than for the tests (`ci_test`), because the test fixture refuses to run against the configured database.

## Known limitations and operational notes

- **One process only.** The rate limiter and the database connection limiter keep their state in memory. After a restart the
  counters are empty, and with several processes or replicas each one counts separately. Run a single API process or container.
- **No TLS.** The API speaks plain HTTP. Put a TLS-terminating reverse proxy in front of it, and configure `TRUSTED_PROXIES` as
  described under "Abuse protection". No CORS headers are sent, so browsers do not let pages from other origins read its responses.
- **Accounts and tokens.** Tokens last 30 minutes, with no refresh and no revocation. There is no password change or reset, email
  verification, account deletion or roles. Registration is open unless you turn it off, and registering a taken name reveals that
  it exists.
- **Tasks.** At most 1,000 per account; no filtering, search or `updated_at`; page totals and pages are not read as one snapshot.
- **Database.** Backups are up to you. Downgrade guards protect only online migrations, not the SQL printed by `--sql`. Renaming the
  Compose project changes the volume name (see "Volume names changed").
- **Health endpoint.** Confirmed from the code: `GET /health/db` needs no token and is not rate limited, and each call briefly
  uses one database connection for a `SELECT 1`. If the API is reachable from the internet, restrict or rate limit that path at
  the reverse proxy. `GET /health` does not touch the database.
- **Login capacity.** Confirmed from the code: login and registration keep their database connection while the password is
  hashed or checked (about 20 ms with the production Argon2 settings on a development machine), so with the default pool at most
  20 password checks run at once. Not tested: whether many addresses together could use this to keep the pool busy, because the
  per-address rate limits do not stop a distributed flood. Other requests would then wait up to `DB_POOL_TIMEOUT_SECONDS` and
  receive the retryable `503`. No attack of this kind has been run against the API. This is a capacity limit, not a way to read
  or change data.
- **`.env` is passed whole.** Confirmed from the Compose file: the API container receives every variable in `.env`, not only the
  ones the application reads, while the migration container receives only the database settings. Keep `.env` to this project's
  settings.
- **Supply chain.** Dependencies and base images are pinned for reproducibility but not scanned or updated automatically.
- **Verification.** The GitHub Actions workflow has not yet run on a public repository (see "Continuous integration"), and
  the project has had no independent security review.

## Security

To report a vulnerability, read [SECURITY.md](SECURITY.md) first. It currently notes that no private reporting channel has been
set up yet, so please do not publish vulnerability details in a public issue.

## License

Released under the [MIT License](LICENSE).
