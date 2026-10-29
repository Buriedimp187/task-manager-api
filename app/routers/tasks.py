from fastapi import APIRouter, HTTPException, Path, status

from app.schemas import NotImplementedResponse, TaskCreate, TaskRead, TaskUpdate

router = APIRouter(
    prefix="/tasks",
    tags=["tasks"],
    responses={
        status.HTTP_501_NOT_IMPLEMENTED: {
            "model": NotImplementedResponse,
            "description": "Task persistence is not implemented yet",
        }
    },
)

POSTGRES_INTEGER_MAX = 2_147_483_647

TaskId = Path(gt=0, le=POSTGRES_INTEGER_MAX, description="Task identifier")


def not_implemented() -> HTTPException:
    # Request validation runs before these handlers; persistence arrives with the database phase.
    return HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Task persistence is not implemented yet",
    )


@router.post("", response_model=TaskRead, status_code=status.HTTP_201_CREATED)
def create_task(task: TaskCreate):
    raise not_implemented()


@router.get("", response_model=list[TaskRead])
def list_tasks():
    raise not_implemented()


@router.get("/{task_id}", response_model=TaskRead)
def get_task(task_id: int = TaskId):
    raise not_implemented()


@router.patch("/{task_id}", response_model=TaskRead)
def update_task(task: TaskUpdate, task_id: int = TaskId):
    raise not_implemented()


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_task(task_id: int = TaskId):
    raise not_implemented()
