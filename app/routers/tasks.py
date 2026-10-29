from fastapi import APIRouter, Depends, HTTPException, Path, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Task, User
from app.schemas import ErrorResponse, TaskCreate, TaskRead, TaskUpdate
from app.security import get_current_user

router = APIRouter(
    prefix="/tasks",
    tags=["tasks"],
    # Every task route requires a valid access token, including any route added later.
    dependencies=[Depends(get_current_user)],
    responses={
        status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse, "description": "Not authenticated"},
        # Produced by the exception handler in app/main.py when the database cannot be reached.
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorResponse, "description": "Database unavailable"},
    },
)

POSTGRES_INTEGER_MAX = 2_147_483_647

TaskId = Path(gt=0, le=POSTGRES_INTEGER_MAX, description="Task identifier")

NOT_FOUND = {status.HTTP_404_NOT_FOUND: {"model": ErrorResponse, "description": "Task not found"}}


def task_not_found() -> HTTPException:
    # Also the answer for a task that belongs to someone else: it must look exactly like a missing one.
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")


def own_task(task_id: int, user: User):
    """A query for one task of this user. The owner test is part of the statement itself, so there is no
    separate check that could be skipped or raced."""
    return select(Task).where(Task.id == task_id, Task.owner_id == user.id)


# Sessions come from get_db, which closes them (rolling back anything uncommitted) when the request
# ends. Handlers that write therefore commit explicitly, and a failure before the commit leaves
# nothing behind.


@router.post("", response_model=TaskRead, status_code=status.HTTP_201_CREATED)
def create_task(data: TaskCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    # The owner comes from the token only; TaskCreate rejects any owner field in the body.
    task = Task(**data.model_dump(), owner_id=user.id)
    db.add(task)
    db.commit()
    return task


@router.get("", response_model=list[TaskRead])
def list_tasks(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return db.scalars(select(Task).where(Task.owner_id == user.id).order_by(Task.id)).all()


@router.get("/{task_id}", response_model=TaskRead, responses=NOT_FOUND)
def get_task(task_id: int = TaskId, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    task = db.scalar(own_task(task_id, user))
    if task is None:
        raise task_not_found()
    return task


@router.patch("/{task_id}", response_model=TaskRead, responses=NOT_FOUND)
def update_task(
    changes: TaskUpdate,
    task_id: int = TaskId,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # FOR UPDATE locks the row until the commit, so a concurrent PATCH or DELETE waits instead of
    # failing: a delete that wins the race simply makes this request a 404.
    task = db.scalar(own_task(task_id, user).with_for_update())
    if task is None:
        raise task_not_found()
    # Only the fields the client sent are applied; an explicit null clears the description.
    for field, value in changes.model_dump(exclude_unset=True).items():
        setattr(task, field, value)
    db.commit()
    return task


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT, responses=NOT_FOUND)
def delete_task(task_id: int = TaskId, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    task = db.scalar(own_task(task_id, user).with_for_update())
    if task is None:
        raise task_not_found()
    db.delete(task)
    db.commit()
