import logging
from contextlib import asynccontextmanager

import anyio.to_thread
from fastapi import Depends, FastAPI, Request, status
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, InternalError, OperationalError, SQLAlchemyError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.orm import Session

from app.body_limit import BodySizeLimitMiddleware
from app.config import get_access_settings, get_auth_settings, get_server_settings
from app.database import DatabaseBusy, dispose_engine, get_db
from app.routers import auth, tasks

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Refuse to start, with a clear message, if SECRET_KEY is missing or too short. Importing the
    # module (as the tests do) still needs no configuration: this only runs when the server starts.
    get_auth_settings()
    get_access_settings()  # likewise for the rate-limit, registration and proxy settings
    # Synchronous endpoints run in a thread pool; its size, with the connection pool, bounds the work in flight.
    anyio.to_thread.current_default_thread_limiter().total_tokens = get_server_settings().request_threads
    try:
        yield
    finally:
        dispose_engine()  # close the pooled connections instead of leaving them for the server to cut


app = FastAPI(title="Task Manager API", lifespan=lifespan)
app.add_middleware(BodySizeLimitMiddleware)
app.include_router(auth.router)
app.include_router(tasks.router)

# SQLSTATE codes meaning the database could not be reached or is shutting down or full. Errors raised
# by the client library before the server answers (refused, timed out, connection lost) carry no
# SQLSTATE at all and count as connectivity failures too.
CONNECTIVITY_SQLSTATES = ("08", "53300", "57P01", "57P02", "57P03")
# The database is up but too busy right now: a row lock or a statement exceeded its timeout (the
# application's own safeguards), or the idle-in-transaction timeout closed the session. A retry may succeed.
OVERLOAD_SQLSTATES = ("55P03", "57014", "25P03")
# Anything else, such as a deadlock, a constraint or a bug in a query, is not an outage and stays a generic 500.
RETRY_AFTER_SECONDS = "1"


def is_connectivity_failure(error: OperationalError) -> bool:
    if error.connection_invalidated:
        return True
    sqlstate = getattr(error.orig, "sqlstate", None)
    return sqlstate is None or sqlstate.startswith(CONNECTIVITY_SQLSTATES)


def is_overload(error: DBAPIError) -> bool:
    sqlstate = getattr(error.orig, "sqlstate", None)
    return sqlstate in OVERLOAD_SQLSTATES


def log_database_problem(message: str, error: Exception) -> None:
    # Log only exception class names: driver messages can include hosts, ports and user names.
    orig = getattr(error, "orig", None)
    logger.warning("%s: %s (driver error: %s)", message, type(error).__name__, type(orig).__name__ if orig else "none")


@app.exception_handler(OperationalError)
async def database_unavailable(request: Request, error: OperationalError):
    if is_connectivity_failure(error):
        log_database_problem("Database unavailable", error)
        return JSONResponse(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content={"detail": "Database unavailable"})
    return busy_or_raise(error)


@app.exception_handler(InternalError)
async def database_internal_error(request: Request, error: InternalError):
    return busy_or_raise(error)


def busy_or_raise(error: DBAPIError):
    if not is_overload(error):
        raise error  # not an outage: let it become the generic 500, with its traceback logged
    log_database_problem("Database busy", error)
    return retryable_503()


@app.exception_handler(DatabaseBusy)
async def database_busy(request: Request, error: DatabaseBusy):
    logger.warning("Database busy: no free slot for the request")
    return retryable_503()


@app.exception_handler(PoolTimeoutError)
async def connection_pool_exhausted(request: Request, error: PoolTimeoutError):
    # Raised when no pooled connection became free within the pool timeout.
    logger.warning("Database busy: %s (no free connection)", type(error).__name__)
    return retryable_503()


def retryable_503() -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": "Database busy, retry shortly"},
        headers={"Retry-After": RETRY_AFTER_SECONDS},
    )


UNENCODABLE_TEXT = "<unencodable text>"


def without_unencodable_text(value):
    """Replace text that cannot be encoded as UTF-8 (a lone surrogate such as "\\ud800").

    FastAPI echoes the rejected input in a 422 body, and a response containing such text cannot be
    serialized, which turned an invalid request into a 500. Anything that can be encoded is left as is.
    """
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return UNENCODABLE_TEXT
        return value
    if isinstance(value, dict):
        return {without_unencodable_text(key): without_unencodable_text(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(without_unencodable_text(item) for item in value)
    return value


# Requests under this prefix carry passwords.
SENSITIVE_PATH_PREFIX = "/auth"


@app.exception_handler(RequestValidationError)
async def invalid_request(request: Request, error: RequestValidationError):
    errors = without_unencodable_text(error.errors())
    if request.url.path.startswith(SENSITIVE_PATH_PREFIX):
        # `input` would echo the submitted value: the password itself, or, for an error about the body as
        # a whole (a missing or unknown field), the entire object including the password.
        errors = [{key: value for key, value in item.items() if key != "input"} for item in errors]
    # Delegates to FastAPI's own handler, so the response is built exactly as before.
    cleaned = RequestValidationError(errors, body=None)
    return await request_validation_exception_handler(request, cleaned)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness: the API process is running. Does not touch the database."""
    return {"status": "ok"}


@app.get("/health/db", responses={503: {"description": "Database unavailable"}})
def health_db(db: Session = Depends(get_db)):
    """Readiness: the API can reach the database."""
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        # Log only exception class names: driver messages can include connection details.
        driver_error = type(exc.orig).__name__ if getattr(exc, "orig", None) else "none"
        logger.warning(
            "Database readiness check failed: %s (driver error: %s)",
            type(exc).__name__,
            driver_error,
        )
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "unavailable"},
        )
    return {"status": "ok"}
