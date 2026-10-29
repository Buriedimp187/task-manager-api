import asyncio
from collections.abc import AsyncIterator, Iterator
from functools import lru_cache
from weakref import WeakKeyDictionary

import anyio
from fastapi import Depends
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings


@lru_cache
def get_engine() -> Engine:
    # create_engine does not connect, so importing the app never needs a running database.
    settings = get_settings()
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,  # a connection dropped by a restart is replaced instead of failing a request
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_seconds,
        pool_recycle=settings.db_pool_recycle_seconds,
        # connect_timeout keeps requests from hanging when the database is unreachable. The timeouts in
        # "options" are server-side defaults: a transaction can still change them for itself with SET LOCAL.
        connect_args={"connect_timeout": settings.db_connect_timeout_seconds, "options": settings.connection_options},
    )


def dispose_engine() -> None:
    """Close every pooled connection, if an engine was ever created."""
    if get_engine.cache_info().currsize:
        get_engine().dispose()


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


class DatabaseBusy(Exception):
    """No database slot became free within the pool timeout."""


# One limiter per event loop (a server has exactly one).
_limiters: WeakKeyDictionary[asyncio.AbstractEventLoop, anyio.Semaphore] = WeakKeyDictionary()


async def admit_request() -> AsyncIterator[None]:
    """Let at most as many requests use the database at once as the pool has connections.

    A request keeps its pooled connection from its first query until its session closes, yet needs a worker
    thread again for each later step. Without a cap, requests holding every connection can wait for threads
    while newer requests hold every thread and wait for a connection: nothing moves until the pool timeout
    fires. Waiting here instead costs no thread, and an admitted request always finds a connection.
    """
    settings = get_settings()
    loop = asyncio.get_running_loop()
    limiter = _limiters.get(loop)
    if limiter is None:
        limiter = _limiters[loop] = anyio.Semaphore(settings.db_pool_size + settings.db_max_overflow)
    try:
        with anyio.fail_after(settings.db_pool_timeout_seconds):
            await limiter.acquire()
    except TimeoutError:
        raise DatabaseBusy from None
    try:
        yield
    finally:
        limiter.release()


def get_db(_admitted: None = Depends(admit_request)) -> Iterator[Session]:
    with get_session_factory()() as session:
        yield session
