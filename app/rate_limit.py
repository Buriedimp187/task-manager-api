"""Rate limiting for registration and login.

The limiter keeps its counters in this process's memory. That is enough for a single server process, which
is how this API is meant to run, but it has two consequences: the counters are lost on restart, and with
several processes (workers or replicas) each one counts separately, so the effective limit is multiplied.
Running more than one process needs a shared store instead.
"""

import ipaddress
import math
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from functools import lru_cache

from fastapi import HTTPException, Request, status

from app.config import get_access_settings
from app.schemas import LoginRequest

# Counters kept at once. Past this, the least recently used ones are dropped, which bounds memory when
# an attacker invents client addresses or usernames.
MAX_TRACKED_KEYS = 10_000


def now() -> float:
    return time.monotonic()


@dataclass(frozen=True)
class Rule:
    limit: int  # attempts allowed ...
    window: float  # ... within this many seconds


class RateLimiter:
    """Sliding-window limiter. Checking and recording happen under one lock, so concurrent attempts cannot
    slip past a limit together."""

    def __init__(self, max_keys: int = MAX_TRACKED_KEYS) -> None:
        self._max_keys = max_keys
        self._lock = threading.Lock()
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()

    def attempt(self, checks: list[tuple[str, Rule]]) -> int | None:
        """Record one attempt against every key, or against none of them if any key is over its limit.

        Returns None when the attempt is allowed, otherwise the whole seconds until it would be."""
        with self._lock:
            moment = now()
            retry_after = 0.0
            for key, rule in checks:
                hits = self._hits.get(key)
                if hits is None:
                    continue
                while hits and hits[0] <= moment - rule.window:
                    hits.popleft()
                if len(hits) >= rule.limit:
                    retry_after = max(retry_after, hits[0] + rule.window - moment)
            if retry_after:
                return max(1, math.ceil(retry_after))
            for key, _ in checks:
                self._hits.setdefault(key, deque()).append(moment)
                self._hits.move_to_end(key)
            while len(self._hits) > self._max_keys:
                self._hits.popitem(last=False)
            return None

    def forget(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)


@lru_cache
def get_limiter() -> RateLimiter:
    return RateLimiter()


def client_address(request: Request) -> str:
    """The address to rate-limit by.

    The connection's own address, unless it belongs to a configured trusted proxy: then the nearest address in
    X-Forwarded-For that is not itself a trusted proxy. Without TRUSTED_PROXIES the header is ignored, because
    any client can send it. IPv6 clients are grouped by /64, the smallest block one customer normally controls.
    """
    peer = request.client.host if request.client else "unknown"
    trusted = get_access_settings().trusted_proxy_networks
    address = _parse(peer)
    if trusted and address is not None and any(address in network for network in trusted):
        forwarded = ",".join(request.headers.getlist("x-forwarded-for")).split(",")
        for entry in reversed(forwarded):
            hop = _parse(entry.strip())
            if hop is None:
                break  # garbage in the chain: nothing in it can be believed
            if not any(hop in network for network in trusted):
                address = hop
                break
    if address is None:
        return peer
    if address.version == 6:
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.ip_network(f"{address}/64", strict=False))
    return str(address)


def _parse(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        return None


def too_many_requests(retry_after: int) -> HTTPException:
    # The same response whichever limit was hit, and whether or not the account exists.
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="Too many requests. Try again later.",
        headers={"Retry-After": str(retry_after)},
    )


def login_user_key(address: str, username: str) -> str:
    return f"login-user:{address}:{username.lower()}"


# These guards are async, so a rejected request costs neither a worker thread nor a database connection, and
# they run before the handler hashes any password.


async def limit_registration(request: Request) -> None:
    settings = get_access_settings()
    if not settings.registration_enabled:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Registration is disabled")
    rule = Rule(settings.register_ip_limit, settings.register_ip_window_seconds)
    retry_after = get_limiter().attempt([(f"register-ip:{client_address(request)}", rule)])
    if retry_after is not None:
        raise too_many_requests(retry_after)


async def limit_login(request: Request, data: LoginRequest) -> LoginRequest:
    settings = get_access_settings()
    address = client_address(request)
    user_key = login_user_key(address, data.username)
    retry_after = get_limiter().attempt(
        [
            (f"login-ip:{address}", Rule(settings.login_ip_limit, settings.login_ip_window_seconds)),
            (user_key, Rule(settings.login_user_limit, settings.login_user_window_seconds)),
        ]
    )
    if retry_after is not None:
        raise too_many_requests(retry_after)
    request.state.login_user_key = user_key
    return data


def forgive_login_failures(request: Request) -> None:
    """After a successful login the failed attempts for that address and username no longer count."""
    get_limiter().forget(request.state.login_user_key)
