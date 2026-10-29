import logging

from fastapi import Depends, FastAPI, status
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.database import get_db
from app.routers import tasks

logger = logging.getLogger(__name__)

app = FastAPI(title="Task Manager API")
app.include_router(tasks.router)


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
