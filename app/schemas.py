from datetime import datetime
from enum import Enum
from typing import Annotated

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
# Text is trimmed first, so a whitespace-only description becomes "" and is then stored as None.
Description = Annotated[
    Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] | None,
    AfterValidator(lambda value: value or None),
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


class NotImplementedResponse(BaseModel):
    """Body of the temporary 501 response returned by the task routes."""

    detail: str


class TaskRead(BaseModel):
    id: int = Field(gt=0)
    title: str
    description: str | None
    status: TaskStatus
    created_at: datetime
