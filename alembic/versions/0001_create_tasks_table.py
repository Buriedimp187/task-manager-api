"""create tasks table

Revision ID: 0001
Revises:
Create Date: 2026-10-28 22:59:31.444846

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Migrations are frozen snapshots: values and names are written out here on purpose
    # instead of being imported from the application code, which will keep changing.
    op.create_table(
        "tasks",
        sa.Column("id", sa.Integer(), sa.Identity(always=False), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.String(length=2000), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="todo", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("char_length(btrim(title)) > 0", name=op.f("ck_tasks_title_not_blank")),
        sa.CheckConstraint(
            "status IN ('todo', 'in_progress', 'done')", name=op.f("ck_tasks_task_status")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tasks")),
    )


def downgrade() -> None:
    # Dropping the table destroys every task in it, so refuse while any task exists. There is no
    # override flag: to discard the data on purpose, delete the rows yourself and run this again.
    #
    # The ACCESS EXCLUSIVE lock is taken before the count and held until this migration's
    # transaction ends, so no other session can insert a row between the count and the DROP:
    # writers wait, then fail once the table is gone.
    #
    # Offline mode (--sql) only prints SQL; it cannot lock the table or look at the data, so it
    # skips the check. The generated script is NOT protected and drops the table unconditionally.
    if not op.get_context().as_sql:
        connection = op.get_bind()
        connection.execute(sa.text("LOCK TABLE tasks IN ACCESS EXCLUSIVE MODE"))
        task_count = connection.execute(sa.text("SELECT count(*) FROM tasks")).scalar_one()
        if task_count:
            raise RuntimeError(
                f"Refusing to drop the tasks table: it contains {task_count} task(s). "
                "Delete them first if you really want to discard the data."
            )
    op.drop_table("tasks")
