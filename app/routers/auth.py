from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import AuthSettings, get_auth_settings
from app.database import get_db
from app.models import User
from app.rate_limit import forgive_login_failures, limit_login, limit_registration
from app.schemas import ErrorResponse, LoginRequest, TokenResponse, UserCreate, UserRead
from app.security import (
    ACCESS_TOKEN_TTL,
    burn_password_check_time,
    create_access_token,
    hash_password,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])

UNIQUE_VIOLATION = "23505"


def username_taken() -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already taken")


def invalid_credentials() -> HTTPException:
    # The same response whether the username exists or not.
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Incorrect username or password",
        headers={"WWW-Authenticate": "Bearer"},
    )


def find_user(db: Session, username: str) -> User | None:
    return db.scalar(select(User).where(func.lower(User.username) == username.lower()))


@router.post(
    "/register",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(limit_registration)],
    responses={
        status.HTTP_403_FORBIDDEN: {"model": ErrorResponse, "description": "Registration is disabled"},
        status.HTTP_409_CONFLICT: {"model": ErrorResponse, "description": "Username already taken"},
        status.HTTP_429_TOO_MANY_REQUESTS: {"model": ErrorResponse, "description": "Too many registration attempts"},
    },
)
def register(data: UserCreate, db: Session = Depends(get_db)):
    if find_user(db, data.username) is not None:
        raise username_taken()
    user = User(username=data.username, password_hash=hash_password(data.password))
    db.add(user)
    try:
        db.commit()
    except IntegrityError as error:
        # PostgreSQL's unique index is the authority: two registrations can both pass the check above.
        if getattr(error.orig, "sqlstate", None) != UNIQUE_VIOLATION:
            raise
        raise username_taken() from None
    return user


@router.post(
    "/login",
    response_model=TokenResponse,
    responses={
        status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse, "description": "Incorrect credentials"},
        status.HTTP_429_TOO_MANY_REQUESTS: {"model": ErrorResponse, "description": "Too many login attempts"},
    },
)
def login(
    request: Request,
    response: Response,
    data: LoginRequest = Depends(limit_login),
    db: Session = Depends(get_db),
    settings: AuthSettings = Depends(get_auth_settings),
):
    user = find_user(db, data.username)
    if user is None:
        burn_password_check_time(data.password)
        raise invalid_credentials()
    if not verify_password(user.password_hash, data.password):
        raise invalid_credentials()
    forgive_login_failures(request)
    response.headers["Cache-Control"] = "no-store"
    return TokenResponse(
        access_token=create_access_token(user.id, settings),
        expires_in=int(ACCESS_TOKEN_TTL.total_seconds()),
    )
