"""Password hashing, access tokens, and the dependency that authenticates a request."""

import uuid
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.config import AuthSettings, get_auth_settings
from app.database import get_db
from app.models import User

ALGORITHM = "HS256"
ACCESS_TOKEN_TTL = timedelta(minutes=30)
REQUIRED_CLAIMS = ("sub", "exp", "iat")
# A token carries exactly these claims, as create_access_token writes them; anything else is refused.
EXPECTED_CLAIMS = frozenset(REQUIRED_CLAIMS)
IAT_LEEWAY_SECONDS = 5  # how far ahead of this server's clock a token's issue time may be
MAX_USER_ID = 2_147_483_647  # the largest value of the users.id column

# Argon2id with the minimum parameters OWASP recommends (19 MiB, 2 passes, 1 lane). Every login costs
# this much memory, and there is no rate limiting yet, so the smaller profile is deliberate.
ARGON2_PARAMETERS = {"memory_cost": 19456, "time_cost": 2, "parallelism": 1}
password_hasher = PasswordHasher(**ARGON2_PARAMETERS)


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return password_hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


# A hash of a random password nobody knows, made with the same parameters as real hashes. It is created once
# at start-up (see the lifespan in app/main.py), not on the first login for an unknown username, so that
# request is not slower than the ones after it.
_dummy_password_hash: str | None = None


def initialize_dummy_password_hash() -> None:
    global _dummy_password_hash
    _dummy_password_hash = hash_password(uuid.uuid4().hex)


def burn_password_check_time(password: str) -> None:
    """Spend the same effort as a real check, so an unknown username is not faster than a wrong password."""
    if _dummy_password_hash is None:
        # Failing is better than quietly making this one request different from all the others.
        raise RuntimeError("the dummy password hash was not initialized at start-up")
    verify_password(_dummy_password_hash, password)


def create_access_token(user_id: int, settings: AuthSettings) -> str:
    now = datetime.now(UTC)
    claims = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + ACCESS_TOKEN_TTL,
    }
    return jwt.encode(claims, settings.secret_key.get_secret_value(), algorithm=ALGORITHM)


def _now() -> int:
    return int(datetime.now(UTC).timestamp())


def decode_access_token(token: str, settings: AuthSettings) -> int | None:
    """Return the user id in a valid token, or None. The reason a token is rejected is never reported.

    The signature and algorithm are checked by PyJWT. The claims are checked here, strictly and in the exact
    form create_access_token writes them, rather than through PyJWT's lenient number handling (which accepts
    numeric strings, floats and booleans for the timestamps).
    """
    try:
        claims = jwt.decode(
            token,
            settings.secret_key.get_secret_value(),
            algorithms=[ALGORITHM],  # pinned: never taken from the token's own header
            options={"require": list(REQUIRED_CLAIMS), "verify_exp": False, "verify_iat": False, "verify_nbf": False},
        )
    except jwt.PyJWTError:
        return None
    if set(claims) != EXPECTED_CLAIMS:
        return None
    issued_at, expires_at = claims["iat"], claims["exp"]
    # `type(...) is int` rather than isinstance: a boolean is an int in Python, and floats and strings are not accepted.
    if type(issued_at) is not int or type(expires_at) is not int:
        return None
    now = _now()
    if issued_at > now + IAT_LEEWAY_SECONDS:  # issued in the future
        return None
    if expires_at <= now:  # expired
        return None
    if not 0 < expires_at - issued_at <= ACCESS_TOKEN_TTL.total_seconds():  # lives longer than this server issues
        return None
    subject = claims["sub"]
    # Exactly what create_access_token writes: ASCII digits, no sign, no spaces, no leading zeros, and a value the
    # users.id column can hold. (int() alone would also accept " 1", "+1" and non-ASCII digits, and the length
    # check comes first because int() refuses thousands of digits.)
    if not (isinstance(subject, str) and subject.isascii() and subject.isdigit() and len(subject) <= 10):
        return None
    user_id = int(subject)
    if str(user_id) != subject or not 0 < user_id <= MAX_USER_ID:
        return None
    return user_id


bearer_scheme = HTTPBearer(auto_error=False, description="Access token from POST /auth/login")


def not_authenticated() -> HTTPException:
    # One response for every failure (missing, malformed, expired, wrongly signed, unknown user).
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
    settings: AuthSettings = Depends(get_auth_settings),
) -> User:
    if credentials is None:
        raise not_authenticated()
    user_id = decode_access_token(credentials.credentials, settings)
    if user_id is None:
        raise not_authenticated()
    user = db.get(User, user_id)
    if user is None:
        raise not_authenticated()
    return user
