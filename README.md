# Task Manager API

A REST API for managing tasks, built with Python, FastAPI, PostgreSQL, SQLAlchemy and Pytest.

Status: early development. The health endpoint works. The `/tasks` routes and their request/response schemas are defined and validated, but return `501 Not Implemented` until the database phase.

## Run locally

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000/docs for the interactive documentation.

## Test

```bash
pytest
```
