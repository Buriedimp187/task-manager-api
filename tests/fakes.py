"""In-memory stand-ins for the database and for authenticated clients.

They keep the default test run fast and independent of PostgreSQL. They check the API contract and
the logic of the handlers, never PostgreSQL behavior: persistence, constraints, locking and
concurrency are covered by the PostgreSQL integration tests.
"""

from datetime import UTC, datetime
from types import SimpleNamespace

from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.sql import operators
from sqlalchemy.sql.elements import BinaryExpression, BindParameter, BooleanClauseList
from sqlalchemy.sql.functions import Function

from app.config import get_auth_settings
from app.models import Task, User
from app.schemas import TaskStatus
from app.security import create_access_token, hash_password


class UniqueViolation(Exception):
    """Stands in for psycopg's UniqueViolation (SQLSTATE 23505)."""

    sqlstate = "23505"


def _value(row, expression):
    if isinstance(expression, BindParameter):
        return expression.value
    if isinstance(expression, Function) and expression.name == "lower":
        (argument,) = list(expression.clauses)
        return _value(row, argument).lower()
    key = getattr(expression, "key", None)
    if key is not None:
        return getattr(row, key)
    raise NotImplementedError(f"FakeSession cannot evaluate {expression!r}")


def _matches(row, clause):
    """Evaluate the WHERE clause the handler really built, so a missing filter changes the outcome."""
    if isinstance(clause, BooleanClauseList):
        results = [_matches(row, part) for part in clause.clauses]
        return all(results) if clause.operator is operators.and_ else any(results)
    if isinstance(clause, BinaryExpression) and clause.operator is operators.eq:
        return _value(row, clause.left) == _value(row, clause.right)
    raise NotImplementedError(f"FakeSession cannot evaluate {clause!r}")


class FakeSession:
    def __init__(self):
        self.rows = {Task: {}, User: {}}  # committed rows by id
        self.pending = []  # added, not committed yet
        self.removed = []  # deleted, not committed yet
        self.next_id = {Task: 1, User: 1}
        self.calls = []
        self.statement = None
        self.conflict_on_commit = False  # simulate losing a registration race to another request

    def _select(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        if entity is None:  # select(func.count()).select_from(Model)
            entity = {"tasks": Task, "users": User}[statement.get_final_froms()[0].name]
        self.statement = str(statement.compile(dialect=postgresql.dialect()))
        rows = sorted(self.rows[entity].values(), key=lambda row: row.id)
        clause = statement.whereclause
        rows = [row for row in rows if clause is None or _matches(row, clause)]
        if statement._offset is not None:
            rows = rows[statement._offset :]
        if statement._limit is not None:
            rows = rows[: statement._limit]
        return entity, rows

    def scalar(self, statement):
        entity, rows = self._select(statement)
        if "count(" in self.statement:
            self.calls.append(f"count:{entity.__name__}")
            return len(rows)
        self.calls.append(f"scalar:{entity.__name__}:for_update={' FOR ' in self.statement}")
        return rows[0] if rows else None

    def scalars(self, statement):
        entity, rows = self._select(statement)
        self.calls.append(f"scalars:{entity.__name__}")
        return SimpleNamespace(all=lambda: rows)

    def get(self, model, key):
        self.calls.append(f"get:{model.__name__}:{key}")
        return self.rows[model].get(key)

    def execute(self, statement):
        self.calls.append("execute")

    def add(self, row):
        self.calls.append(f"add:{type(row).__name__}")
        self.pending.append(row)

    def delete(self, row):
        self.calls.append("delete")
        self.removed.append(row)

    def rollback(self):
        self.calls.append("rollback")
        self.pending.clear()
        self.removed.clear()

    def commit(self):
        self.calls.append("commit")
        try:
            for row in self.pending:
                model = type(row)
                taken = model is User and any(
                    user.username.lower() == row.username.lower() for user in self.rows[User].values()
                )
                if taken or self.conflict_on_commit:
                    raise IntegrityError("INSERT INTO users", {}, UniqueViolation())
                row.id = self.next_id[model]
                self.next_id[model] += 1
                row.created_at = datetime.now(UTC)
                if model is Task:
                    row.status = row.status or TaskStatus.todo
                self.rows[model][row.id] = row
            for row in self.removed:
                self.rows[type(row)].pop(row.id, None)
        finally:
            self.pending.clear()
            self.removed.clear()

    def add_user(self, username, password="a perfectly fine password"):
        """Put an account straight into the fake, as if it had registered earlier."""
        user = User(username=username, password_hash=hash_password(password))
        user.id = self.next_id[User]
        self.next_id[User] += 1
        user.created_at = datetime.now(UTC)
        self.rows[User][user.id] = user
        return user


def bearer_headers(user_id):
    """A genuine access token for `user_id`, signed with the secret the test run is using."""
    return {"Authorization": f"Bearer {create_access_token(user_id, get_auth_settings())}"}
