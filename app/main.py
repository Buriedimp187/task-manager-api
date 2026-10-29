from fastapi import FastAPI

app = FastAPI(title="Task Manager API")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
