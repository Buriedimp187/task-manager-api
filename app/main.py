from fastapi import FastAPI

from app.routers import tasks

app = FastAPI(title="Task Manager API")
app.include_router(tasks.router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
