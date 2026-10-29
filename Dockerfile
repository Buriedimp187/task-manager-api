# Production image for the Task Manager API.
#
# Base images are pinned by digest so a build is reproducible; update the digest deliberately (it is the
# python:3.13-slim image) when taking a new Python or OS security release.

# --- build stage: installs the dependencies into a virtual environment ------------------------------------------
FROM python:3.13-slim@sha256:2d44738ebf4ce5f63b9d9ea871e9c7cfdc0426f7b0c97cac915fd88ded1d86bb AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY requirements.txt constraints.txt ./
# Runtime requirements only (no pytest, httpx or ruff), with every version fixed by constraints.txt. All
# packages ship wheels, so no compiler is needed. pip is removed afterwards: nothing installs at run time.
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install -r requirements.txt -c constraints.txt \
    && /opt/venv/bin/pip uninstall --yes pip

# --- final stage: only the virtual environment and the application ---------------------------------------------------
FROM python:3.13-slim@sha256:2d44738ebf4ce5f63b9d9ea871e9c7cfdc0426f7b0c97cac915fd88ded1d86bb

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# An unprivileged account with no home directory and no login shell.
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin app

COPY --from=builder /opt/venv /opt/venv

WORKDIR /srv
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
# Code is owned by root and read-only to the application, which also cannot write anywhere else.
RUN python -m compileall -q app alembic

USER 10001:10001
EXPOSE 8000

# Readiness, not just liveness: /health/db answers 200 only while the database can be reached. Uses the standard
# library so the image needs no curl.
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/db', timeout=3)"]

# Exec form: uvicorn is PID 1 and receives SIGTERM directly, finishes in-flight requests (up to 15 s), runs the
# application's shutdown (closing the connection pool) and exits 0. One process only: the rate limiter and the
# connection admission limiter keep their counters in memory. --no-proxy-headers: uvicorn must not trust
# X-Forwarded-For on its own; set TRUSTED_PROXIES instead if the API runs behind a reverse proxy.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers", "--no-server-header", "--timeout-graceful-shutdown", "15"]
