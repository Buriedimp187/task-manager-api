# Task Manager API

A REST API for managing tasks, built with Python, FastAPI, PostgreSQL, SQLAlchemy and Pytest.

Status: early development. Only the health endpoint exists so far.

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
