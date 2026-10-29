# Task Manager API

A REST API for managing tasks, built with Python, FastAPI, PostgreSQL, SQLAlchemy and Pytest.

Status: early development. People can register, log in, and manage their own tasks (create, list, get, update, delete). Data is stored in PostgreSQL, the schema is managed by Alembic migrations, and there are health endpoints. Each task belongs to the account that created it, and no one else can see or change it. Not done yet: rate limiting, password reset, token revocation, containers and CI.

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

### Signing key

The API signs login tokens with `SECRET_KEY` from `.env`. It must be at least 32 characters, and the API **refuses to start** with a clear message if it is missing or too short (the value is never printed). The placeholder in `.env.example` is deliberately too short to work. Generate a real one with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Keep it secret and out of Git: anyone who knows it can forge a login for any account. Changing it invalidates every token that was issued, so everyone has to log in again. Alembic does not need it, so migrations run without a signing key.

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

Open http://127.0.0.1:8000/docs for the interactive documentation. Register and log in with the `/auth` endpoints, then use **Authorize** and paste the `access_token`.

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
| `REGISTRATION_ENABLED` | `true` | `false` makes `POST /auth/register` answer `403`; login is unaffected. Set it to `false` in production once the accounts exist. |
| `TRUSTED_PROXIES` | empty | Comma-separated addresses or networks of reverse proxies whose `X-Forwarded-For` header is believed. |

Over the limit the API answers `429 {"detail": "Too many requests. Try again later."}` with a `Retry-After` header. The response is identical whichever limit was hit and whether or not the account exists, and a correct password is refused too while limited. IPv6 clients are grouped by /64.

**Limitations and deployment.**

- Counters live in this process's memory: they reset on restart, and with several processes or replicas each counts separately, so the real limit is multiplied. Run one process, or add a shared store.
- Clients are identified by their connection address. `X-Forwarded-For` is ignored unless the connection comes from a `TRUSTED_PROXIES` entry; then the nearest address in the header that is not itself a trusted proxy is used.
- **Start uvicorn with `--no-proxy-headers`.** By default uvicorn rewrites the client address from `X-Forwarded-For` for connections from `127.0.0.1`, which lets a local client evade the limits (verified). Behind a reverse proxy, use `--no-proxy-headers` together with `TRUSTED_PROXIES`.
- Behind a reverse proxy, list the proxy in `TRUSTED_PROXIES` (and keep `--no-proxy-headers`). Without it every user appears to come from the proxy's address and shares one set of limits. The proxy must set or append `X-Forwarded-For` itself, not pass a client's value through unchanged. Do not set uvicorn's `FORWARDED_ALLOW_IPS` or pass `--proxy-headers`: they make uvicorn trust the header independently of this setting.
- Many clients behind one address (a company NAT) share its limits.

**Usernames** are 3 to 32 characters: ASCII letters, digits, `_`, `-` and `.`. They are unique **ignoring case**, enforced by PostgreSQL itself (`Alice` and `alice` cannot both exist, even if two registrations arrive at the same moment). The case you chose is kept, and logging in ignores case. **Passwords** are 10 to 128 characters and are used exactly as typed (not trimmed). They are stored only as an Argon2id hash (19 MiB, 2 passes, 1 lane, the minimum OWASP recommends) and are never returned or logged.

**What a client sees**

| Situation | Response |
|---|---|
| Wrong password, or a username that does not exist | `401 {"detail": "Incorrect username or password"}`, identical in both cases (and a comparable amount of work is done, so timing does not tell them apart) |
| Missing, malformed, expired, wrongly signed or otherwise invalid token, or an account that no longer exists | `401 {"detail": "Not authenticated"}` with `WWW-Authenticate: Bearer`. The reason is never given. |
| Registering a name that is taken, in any letter case | `409 {"detail": "Username already taken"}` (registration necessarily reveals this) |
| Invalid registration or login body | `422` **without the `input` field**, so a submitted password is never echoed back, not even inside an error about a missing field |

Tokens are signed with HS256. The server fixes the accepted algorithm and requires the `sub`, `iat` and `exp` claims, so unsigned tokens, tokens signed with another key or algorithm, and tokens missing a claim are all refused.

**Limitations.** There is **no rate limiting**, so logins can be tried without limit and each one costs CPU and memory to hash. Registration is open to anyone. There is no password change or reset, email verification, account deletion or roles. A token cannot be revoked before it expires (logging out is up to the client).

## Tasks API

All request and response bodies are JSON. **Every request needs a token** (see Authentication). Each task belongs to the account that created it, and you only ever see and change your own.

| Request | Purpose | Success | Other responses |
|---|---|---|---|
| `POST /tasks` | Create a task | `201` with the task | `401`, `422` |
| `GET /tasks` | List your tasks, ordered by `id` ascending | `200` with an array (empty if there are none) | `401` |
| `GET /tasks/{id}` | Fetch one of your tasks | `200` with the task | `401`, `404`, `422` |
| `PATCH /tasks/{id}` | Change some fields of one of your tasks | `200` with the updated task | `401`, `404`, `422` |
| `DELETE /tasks/{id}` | Delete one of your tasks | `204` with no body | `401`, `404`, `422` |

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
| `422` | FastAPI's standard validation details, which echo the rejected value in `input` (except under `/auth`, see above) | Invalid body or id. Nothing was changed. Text that cannot be encoded as UTF-8, such as a lone surrogate (`"\ud800"` in JSON), is shown as `<unencodable text>` instead of being echoed. |
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
