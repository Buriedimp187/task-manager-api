from fastapi import APIRouter, Depends, HTTPException, Path, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Task
from app.schemas import ErrorResponse, TaskCreate, TaskRead, TaskUpdate

router = APIRouter(
    prefix="/tasks",
    tags=["tasks"],
    # Produced by the exception handler in app/main.py when the database cannot be reached.
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorResponse, "description": "Database unavailable"}
    },
)

POSTGRES_INTEGER_MAX = 2_147_483_647

TaskId = Path(gt=0, le=POSTGRES_INTEGER_MAX, description="Task identifier")

NOT_FOUND = {status.HTTP_404_NOT_FOUND: {"model": ErrorResponse, "description": "Task not found"}}


def task_not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")


# Sessions come from get_db, which closes them (rolling back anything uncommitted) when the request
# ends. Handlers that write therefore commit explicitly, and a failure before the commit leaves
# nothing behind.


@router.post("", response_model=TaskRead, status_code=status.HTTP_201_CREATED)
def create_task(data: TaskCreate, db: Session = Depends(get_db)):
    task = Task(**data.model_dump())
    db.add(task)
    db.commit()
    return task


@router.get("", response_model=list[TaskRead])
def list_tasks(db: Session = Depends(get_db)):
    return db.scalars(select(Task).order_by(Task.id)).all()


@router.get("/{task_id}", response_model=TaskRead, responses=NOT_FOUND)
def get_task(task_id: int = TaskId, db: Session = Depends(get_db)):
    task = db.get(Task, task_id)
    if task is None:
        raise task_not_found()
    return task


@router.patch("/{task_id}", response_model=TaskRead, responses=NOT_FOUND)
def update_task(changes: TaskUpdate, task_id: int = TaskId, db: Session = Depends(get_db)):
    # FOR UPDATE locks the row until the commit, so a concurrent PATCH or DELETE waits instead of
    # failing: a delete that wins the race simply makes this request a 404.
    task = db.get(Task, task_id, with_for_update=True)
    if task is None:
        raise task_not_found()
    # Only the fields the client sent are applied; an explicit null clears the description.
    for field, value in changes.model_dump(exclude_unset=True).items():
        setattr(task, field, value)
    db.commit()
    return task


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT, responses=NOT_FOUND)
def delete_task(task_id: int = TaskId, db: Session = Depends(get_db)):
    task = db.get(Task, task_id, with_for_update=True)
    if task is None:
        raise task_not_found()
    db.delete(task)
    db.commit()
