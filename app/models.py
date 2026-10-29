from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Identity,
    Index,
    MetaData,
    String,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.schemas import TaskStatus

# Explicit constraint names keep Alembic migrations identical across environments.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        # Mirrors the API rule. Restricting usernames to ASCII also keeps lower() below predictable:
        # PostgreSQL's case folding of other scripts depends on the database locale.
        CheckConstraint("username ~ '^[A-Za-z0-9_.-]{3,32}$'", name="username_format"),
        # Case-insensitive uniqueness, enforced by PostgreSQL itself so concurrent registrations
        # cannot both succeed.
        Index("uq_users_username_lower", text("lower(username)"), unique=True),
    )

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    username: Mapped[str] = mapped_column(String(32))
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        # Mirrors the API rule (non-blank title) so rows written outside the API stay valid.
        CheckConstraint("char_length(btrim(title)) > 0", name="title_not_blank"),
        # Serves the owner-scoped list, which is also ordered by id.
        Index("ix_tasks_owner_id_id", "owner_id", "id"),
    )

    # Identity() makes a 32-bit column; the task routes already cap ids at the same maximum.
    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    # Set from the authenticated user, never from a request body. Tasks go with their owner.
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(String(2000))
    # Stored as VARCHAR + CHECK, not a native PostgreSQL enum: adding a status later is a plain
    # constraint change instead of ALTER TYPE.
    status: Mapped[TaskStatus] = mapped_column(
        Enum(
            TaskStatus,
            name="task_status",
            native_enum=False,
            create_constraint=True,
            length=20,
            values_callable=lambda statuses: [status.value for status in statuses],
        ),
        server_default=TaskStatus.todo.value,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
