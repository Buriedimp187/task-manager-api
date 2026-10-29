from datetime import datetime
from enum import Enum
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)


def reject_nul(value: str) -> str:
    # PostgreSQL text cannot contain NUL, so such a value would otherwise fail later, at insert time.
    if "\x00" in value:
        raise ValueError("must not contain NUL characters")
    return value


Title = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
    AfterValidator(reject_nul),
]
# Text is trimmed first, so a whitespace-only description becomes "" and is then stored as None.
Description = Annotated[
    Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000), AfterValidator(reject_nul)]
    | None,
    AfterValidator(lambda value: value or None),
]


# ASCII only, so the database's case-insensitive uniqueness (lower(username)) is unambiguous.
# Mirrored by a CHECK constraint on users.username.
Username = Annotated[str, StringConstraints(min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_.-]+$")]
# Not trimmed: a password is used exactly as typed. The upper bound keeps hashing cost bounded.
# (Pydantic's constrained strings also reject a lone surrogate, which could not be hashed; a test
# pins that, so it cannot silently change into a 500.)
Password = Annotated[
    str,
    StringConstraints(min_length=10, max_length=128),
    AfterValidator(reject_nul),
]


class TaskStatus(str, Enum):
    todo = "todo"
    in_progress = "in_progress"
    done = "done"


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Title
    description: Description = None
    status: TaskStatus = TaskStatus.todo


class TaskUpdate(BaseModel):
    """Partial update: only the fields that are sent are changed."""

    model_config = ConfigDict(extra="forbid")

    title: Title | None = None
    description: Description = None
    status: TaskStatus | None = None

    @model_validator(mode="after")
    def check_update(self) -> "TaskUpdate":
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided")
        # description may be null (to clear it); title and status may not.
        for name in ("title", "status"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self


class UserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: Username
    password: Password


class LoginRequest(BaseModel):
    """Deliberately lenient: the rules for new passwords must not reveal anything about existing ones."""

    model_config = ConfigDict(extra="forbid")

    username: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    password: Annotated[str, StringConstraints(min_length=1, max_length=1024)]


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int = Field(gt=0)
    username: str
    created_at: datetime


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int = Field(description="Seconds until the token expires")


class ErrorResponse(BaseModel):
    """Body of an error response such as 404 or 503."""

    detail: str


class TaskRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int = Field(gt=0)
    title: str
    description: str | None
    status: TaskStatus
    created_at: datetime
