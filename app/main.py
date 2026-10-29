import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request, status
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import get_auth_settings
from app.database import get_db
from app.routers import auth, tasks

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Refuse to start, with a clear message, if SECRET_KEY is missing or too short. Importing the
    # module (as the tests do) still needs no configuration: this only runs when the server starts.
    get_auth_settings()
    yield


app = FastAPI(title="Task Manager API", lifespan=lifespan)
app.include_router(auth.router)
app.include_router(tasks.router)

# SQLSTATE codes meaning the database could not be reached or is shutting down or full. Errors raised
# by the client library before the server answers (refused, timed out, connection lost) carry no
# SQLSTATE at all and count as connectivity failures too. Anything else, such as a deadlock or a lock
# timeout, is not an outage and stays a generic 500.
CONNECTIVITY_SQLSTATES = ("08", "53300", "57P01", "57P02", "57P03")


def is_connectivity_failure(error: OperationalError) -> bool:
    if error.connection_invalidated:
        return True
    sqlstate = getattr(error.orig, "sqlstate", None)
    return sqlstate is None or sqlstate.startswith(CONNECTIVITY_SQLSTATES)


@app.exception_handler(OperationalError)
async def database_unavailable(request: Request, error: OperationalError):
    if not is_connectivity_failure(error):
        raise error  # not an outage: let it become the generic 500, with its traceback logged
    # Log only exception class names: driver messages can include hosts, ports and user names.
    logger.warning(
        "Database unavailable: %s (driver error: %s)",
        type(error).__name__,
        type(error.orig).__name__ if error.orig is not None else "none",
    )
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": "Database unavailable"},
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
