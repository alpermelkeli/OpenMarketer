"""follow up on a writing run

The orchestrator's chat now starts a writing run of its own (``start_writing``), and a writing
run a chat started tells that chat when it ends, in a follow-up turn of the trigger
``writing_finished``. Such a turn names its run in the new ``writing_run_id``, without a foreign
key, as the other follow-ups name theirs. The trigger's CHECK takes the value, and a new CHECK
ties ``writing_run_id`` to it.

A downgrade deletes the writing follow-ups with their events (their answers stay, as messages
of no turn), then puts the old trigger CHECK back and drops the column.

Revision ID: 69a1b7f201ea
Revises: a09e0cc156a8
Create Date: 2026-11-12 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "69a1b7f201ea"
down_revision: str | None = "a09e0cc156a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("chat_turn", sa.Column("writing_run_id", sa.Uuid(), nullable=True))
    op.drop_constraint(op.f("ck_chat_turn_trigger"), "chat_turn", type_="check")
    op.create_check_constraint(
        op.f("ck_chat_turn_trigger"),
        "chat_turn",
        "trigger IN ('message', 'research_finished', 'studio_finished', 'writing_finished', "
        "'task_finished', 'task_step_finished')",
    )
    op.create_check_constraint(
        op.f("ck_chat_turn_writing_run_id"),
        "chat_turn",
        "(trigger = 'writing_finished') = (writing_run_id IS NOT NULL)",
    )


def downgrade() -> None:
    writing_turns = "SELECT id FROM chat_turn WHERE trigger = 'writing_finished'"
    op.execute(f"UPDATE chat_message SET turn_id = NULL WHERE turn_id IN ({writing_turns})")
    op.execute(
        f"DELETE FROM run_event WHERE run_kind = 'orchestrator-chat' "
        f"AND run_id IN ({writing_turns})"
    )
    op.execute("DELETE FROM chat_turn WHERE trigger = 'writing_finished'")
    op.drop_constraint(op.f("ck_chat_turn_writing_run_id"), "chat_turn", type_="check")
    op.drop_constraint(op.f("ck_chat_turn_trigger"), "chat_turn", type_="check")
    op.create_check_constraint(
        op.f("ck_chat_turn_trigger"),
        "chat_turn",
        "trigger IN ('message', 'research_finished', 'studio_finished', 'task_finished', "
        "'task_step_finished')",
    )
    op.drop_column("chat_turn", "writing_run_id")
