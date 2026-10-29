"""Password hashing, access tokens, and the dependency that authenticates a request."""

import uuid
from datetime import UTC, datetime, timedelta
from functools import lru_cache

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


@lru_cache
def _dummy_hash() -> str:
    return hash_password(uuid.uuid4().hex)


def burn_password_check_time(password: str) -> None:
    """Spend the same effort as a real check, so an unknown username is not faster than a wrong password."""
    verify_password(_dummy_hash(), password)


def create_access_token(user_id: int, settings: AuthSettings) -> str:
    now = datetime.now(UTC)
    claims = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + ACCESS_TOKEN_TTL,
    }
    return jwt.encode(claims, settings.secret_key.get_secret_value(), algorithm=ALGORITHM)


def decode_access_token(token: str, settings: AuthSettings) -> int | None:
    """Return the user id in a valid token, or None. The reason a token is rejected is never reported."""
    try:
        claims = jwt.decode(
            token,
            settings.secret_key.get_secret_value(),
            algorithms=[ALGORITHM],  # pinned: never taken from the token's own header
            options={"require": list(REQUIRED_CLAIMS)},
        )
    except jwt.PyJWTError:
        return None
    subject = claims["sub"]
    # Exactly what create_access_token writes: ASCII digits, no sign, no spaces, no leading zeros.
    # (int() alone would also accept " 1", "+1" and non-ASCII digits.)
    if not (isinstance(subject, str) and subject.isascii() and subject.isdigit() and str(int(subject)) == subject):
        return None
    user_id = int(subject)
    return user_id if user_id > 0 else None


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
