"""follow up on each step of a task

A task a chat started now tells that chat about each step that ended while the task goes on,
in a follow-up turn of the trigger ``task_step_finished``. Such a turn names its task in
``task_id``, as a ``task_finished`` one does, and the step in the new ``task_step_key`` (the
step's id in the task's plan, as ``orchestrator_task_step.step_key`` holds it), so each step is
followed up on once. The trigger's CHECK takes the value, ``task_id`` is tied to both task
triggers and ``task_step_key`` to the new one.

A downgrade deletes the step follow-ups with their events (their answers stay, as messages of
no turn), then puts the old CHECKs back and drops the column.

Revision ID: a48df96b2004
Revises: 86a789f035d3
Create Date: 2026-11-05 12:19:38.897763
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a48df96b2004"
down_revision: str | None = "86a789f035d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The longest step id a plan may give (``plan.MAX_STEP_ID_CHARS``) when this was written.
MAX_STEP_ID_CHARS = 32


def upgrade() -> None:
    op.add_column("chat_turn", sa.Column("task_step_key", sa.Text(), nullable=True))
    op.drop_constraint(op.f("ck_chat_turn_trigger"), "chat_turn", type_="check")
    op.create_check_constraint(
        op.f("ck_chat_turn_trigger"),
        "chat_turn",
        "trigger IN ('message', 'research_finished', 'studio_finished', 'task_finished', "
        "'task_step_finished')",
    )
    op.drop_constraint(op.f("ck_chat_turn_task_id"), "chat_turn", type_="check")
    op.create_check_constraint(
        op.f("ck_chat_turn_task_id"),
        "chat_turn",
        "(trigger IN ('task_finished', 'task_step_finished')) = (task_id IS NOT NULL)",
    )
    op.create_check_constraint(
        op.f("ck_chat_turn_task_step_key"),
        "chat_turn",
        "(trigger = 'task_step_finished') = (task_step_key IS NOT NULL)",
    )
    op.create_check_constraint(
        op.f("ck_chat_turn_task_step_key_length"),
        "chat_turn",
        f"char_length(task_step_key) BETWEEN 1 AND {MAX_STEP_ID_CHARS}",
    )


def downgrade() -> None:
    step_turns = "SELECT id FROM chat_turn WHERE trigger = 'task_step_finished'"
    op.execute(f"UPDATE chat_message SET turn_id = NULL WHERE turn_id IN ({step_turns})")
    op.execute(
        f"DELETE FROM run_event WHERE run_kind = 'orchestrator-chat' AND run_id IN ({step_turns})"
    )
    op.execute("DELETE FROM chat_turn WHERE trigger = 'task_step_finished'")
    op.drop_constraint(op.f("ck_chat_turn_task_step_key_length"), "chat_turn", type_="check")
    op.drop_constraint(op.f("ck_chat_turn_task_step_key"), "chat_turn", type_="check")
    op.drop_constraint(op.f("ck_chat_turn_task_id"), "chat_turn", type_="check")
    op.create_check_constraint(
        op.f("ck_chat_turn_task_id"),
        "chat_turn",
        "(trigger = 'task_finished') = (task_id IS NOT NULL)",
    )
    op.drop_constraint(op.f("ck_chat_turn_trigger"), "chat_turn", type_="check")
    op.create_check_constraint(
        op.f("ck_chat_turn_trigger"),
        "chat_turn",
        "trigger IN ('message', 'research_finished', 'studio_finished', 'task_finished')",
    )
    op.drop_column("chat_turn", "task_step_key")
