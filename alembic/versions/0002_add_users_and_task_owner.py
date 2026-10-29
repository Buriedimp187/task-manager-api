"""add users and task owner

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-29 01:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Every task must have an owner, and an owner cannot be invented: guessing one would hand someone
    # else's data to the wrong account. So this refuses to run while any task exists, instead of
    # assigning owners or deleting anything.
    #
    # The ACCESS EXCLUSIVE lock is held until this transaction ends, so no task can be added between
    # the count and the change.
    #
    # Offline mode (--sql) only prints SQL and cannot look at the data. The script it prints is still
    # safe on this point: PostgreSQL rejects ADD COLUMN ... NOT NULL on a table that has rows.
    if not op.get_context().as_sql:
        connection = op.get_bind()
        connection.execute(sa.text("LOCK TABLE tasks IN ACCESS EXCLUSIVE MODE"))
        task_count = connection.execute(sa.text("SELECT count(*) FROM tasks")).scalar_one()
        if task_count:
            raise RuntimeError(
                f"Cannot add tasks.owner_id: the tasks table contains {task_count} task(s) and none of them has "
                "an owner. This migration never invents owners and never discards data. Export or delete "
                "these tasks first (for example, in a psql session: DELETE FROM tasks;), then run "
                "'alembic upgrade head' again."
            )

    # Migrations are frozen snapshots: values and names are written out here on purpose.
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), sa.Identity(always=False), nullable=False),
        sa.Column("username", sa.String(length=32), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("username ~ '^[A-Za-z0-9_.-]{3,32}$'", name=op.f("ck_users_username_format")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
    )
    # Case-insensitive uniqueness, enforced by PostgreSQL.
    op.create_index("uq_users_username_lower", "users", [sa.text("lower(username)")], unique=True)

    op.add_column("tasks", sa.Column("owner_id", sa.Integer(), nullable=False))
    op.create_foreign_key(
        op.f("fk_tasks_owner_id_users"), "tasks", "users", ["owner_id"], ["id"], ondelete="CASCADE"
    )
    op.create_index("ix_tasks_owner_id_id", "tasks", ["owner_id", "id"])


def downgrade() -> None:
    # Dropping `users` destroys every account and password hash, and dropping `owner_id` loses who owns
    # which task. A task cannot exist without an owner, so while any account exists this refuses.
    # There is no override flag: to discard the data on purpose, delete the rows yourself first.
    #
    # Both tables are locked until this transaction ends, so nothing can be registered or created
    # between the count and the drops.
    #
    # Offline mode (--sql) only prints SQL; it cannot lock the tables or look at the data, so it skips
    # the check. The generated script is NOT protected and drops them unconditionally.
    if not op.get_context().as_sql:
        connection = op.get_bind()
        connection.execute(sa.text("LOCK TABLE users, tasks IN ACCESS EXCLUSIVE MODE"))
        user_count = connection.execute(sa.text("SELECT count(*) FROM users")).scalar_one()
        if user_count:
            task_count = connection.execute(sa.text("SELECT count(*) FROM tasks")).scalar_one()
            raise RuntimeError(
                f"Refusing to drop the users table: it contains {user_count} account(s), and {task_count} "
                "task(s) belong to them. Delete them first if you really want to discard the data "
                "(in a psql session: DELETE FROM tasks; DELETE FROM users;)."
            )

    op.drop_index("ix_tasks_owner_id_id", table_name="tasks")
    op.drop_constraint(op.f("fk_tasks_owner_id_users"), "tasks", type_="foreignkey")
    op.drop_column("tasks", "owner_id")
    op.drop_index("uq_users_username_lower", table_name="users")
    op.drop_table("users")
