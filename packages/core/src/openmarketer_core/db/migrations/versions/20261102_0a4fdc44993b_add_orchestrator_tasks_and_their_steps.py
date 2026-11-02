"""add orchestrator tasks and their steps

``orchestrator_task`` holds each task of the orchestrator: the request and the chat's
understanding of it, the plan and its changes, the report and how the task came out, the cost
of its own model calls and the chat that started it; it moves forwards only, under CHECKs and
no trigger.
``orchestrator_task_step`` holds each dispatched step: its id in the plan, its agent, the run
it started (named without a foreign key, as a run event names its run) and that run's outcome.

``writing_run`` gains ``origin_action_id``, as research and studio runs have: a writing run a
task's action asks for is keyed by that action, so a retried attempt makes one run.
``chat_turn`` gains ``task_id`` and the trigger ``task_finished``, the chat's follow-up on a
task it started. ``proposal`` accepts the effect ``confirms``: the gate holds a task's plan for a
person's confirmation as it holds an action that publishes.

Autogenerate wrote each enum's CHECK twice, once in the column, and saw none of
the changed CHECKs of ``chat_turn`` and ``proposal``; both are put right here. The downgrade
drops every task with its steps, the follow-up turns on tasks with their events, the events
and proposals of tasks, and which action asked for each writing run.

Revision ID: 0a4fdc44993b
Revises: 2530b8dce085
Create Date: 2026-11-02 16:09:14.067343
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0a4fdc44993b"
down_revision: str | None = "2530b8dce085"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "orchestrator_task",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "queued",
                "running",
                "succeeded",
                "failed",
                name="status",
                native_enum=False,
                length=32,
            ),
            server_default="queued",
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("requested_by", sa.Text(), server_default="orchestrator", nullable=False),
        sa.Column("request", sa.Text(), nullable=False),
        sa.Column("understanding", sa.Text(), nullable=False),
        sa.Column("plan", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "plan_changes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("report", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "outcome",
            sa.Enum(
                "completed",
                "incomplete",
                "plan_rejected",
                "plan_expired",
                name="outcome",
                native_enum=False,
                length=32,
            ),
            nullable=True,
        ),
        sa.Column(
            "cost_usd",
            sa.Numeric(precision=14, scale=6),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("chat_thread_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(status = 'failed') = (error IS NOT NULL)", name=op.f("ck_orchestrator_task_error")
        ),
        sa.CheckConstraint(
            "(status = 'succeeded') = (report IS NOT NULL)",
            name=op.f("ck_orchestrator_task_report"),
        ),
        sa.CheckConstraint(
            "(status = 'succeeded') = (outcome IS NOT NULL)",
            name=op.f("ck_orchestrator_task_outcome_of_end"),
        ),
        sa.CheckConstraint(
            "outcome IN ('completed', 'incomplete', 'plan_rejected', 'plan_expired')",
            name=op.f("ck_orchestrator_task_outcome"),
        ),
        sa.CheckConstraint(
            "(status IN ('succeeded', 'failed')) = (finished_at IS NOT NULL)",
            name=op.f("ck_orchestrator_task_finished_at"),
        ),
        sa.CheckConstraint(
            "requested_by IN ('person', 'orchestrator', 'run')",
            name=op.f("ck_orchestrator_task_requested_by"),
        ),
        sa.CheckConstraint(
            "status <> 'succeeded' OR plan IS NOT NULL", name=op.f("ck_orchestrator_task_plan")
        ),
        sa.CheckConstraint(
            "status = 'failed' OR (status = 'queued') = (started_at IS NULL)",
            name=op.f("ck_orchestrator_task_started_at"),
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name=op.f("ck_orchestrator_task_status"),
        ),
        sa.CheckConstraint(
            "char_length(error) <= 2000", name=op.f("ck_orchestrator_task_error_length")
        ),
        sa.CheckConstraint("cost_usd >= 0", name=op.f("ck_orchestrator_task_cost_usd")),
        sa.ForeignKeyConstraint(
            ["chat_thread_id"],
            ["chat_thread.id"],
            name=op.f("fk_orchestrator_task_chat_thread_id_chat_thread"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["project.id"], name=op.f("fk_orchestrator_task_project_id_project")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_orchestrator_task")),
        sa.UniqueConstraint("id", "project_id", name=op.f("uq_orchestrator_task_id_project_id")),
    )
    op.create_index(
        op.f("ix_orchestrator_task_chat_thread_id"),
        "orchestrator_task",
        ["chat_thread_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_orchestrator_task_project_id"), "orchestrator_task", ["project_id"], unique=False
    )
    op.create_index(
        "ix_orchestrator_task_project_id_created_at",
        "orchestrator_task",
        ["project_id", "created_at", "id"],
        unique=False,
    )
    op.create_table(
        "orchestrator_task_step",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("step_key", sa.Text(), nullable=False),
        sa.Column("agent", sa.Text(), nullable=False),
        sa.Column("child_run_id", sa.Uuid(), nullable=False),
        sa.Column(
            "outcome",
            sa.Enum(
                "succeeded",
                "needs_review",
                "failed",
                name="outcome",
                native_enum=False,
                length=32,
            ),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "agent IN ('research', 'writing', 'studio')",
            name=op.f("ck_orchestrator_task_step_agent"),
        ),
        sa.CheckConstraint(
            "outcome <> 'needs_review' OR agent = 'studio'",
            name=op.f("ck_orchestrator_task_step_needs_review_of_studio"),
        ),
        sa.CheckConstraint(
            "outcome IN ('succeeded', 'needs_review', 'failed')",
            name=op.f("ck_orchestrator_task_step_outcome"),
        ),
        sa.CheckConstraint(
            "(outcome IS NULL) = (finished_at IS NULL)",
            name=op.f("ck_orchestrator_task_step_finished_at"),
        ),
        sa.CheckConstraint(
            "char_length(step_key) BETWEEN 1 AND 32",
            name=op.f("ck_orchestrator_task_step_step_key"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            name=op.f("fk_orchestrator_task_step_project_id_project"),
        ),
        sa.ForeignKeyConstraint(
            ["task_id", "project_id"],
            ["orchestrator_task.id", "orchestrator_task.project_id"],
            name=op.f("fk_orchestrator_task_step_task_id_orchestrator_task"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_orchestrator_task_step")),
        sa.UniqueConstraint("child_run_id", name=op.f("uq_orchestrator_task_step_child_run_id")),
        sa.UniqueConstraint(
            "task_id", "step_key", name=op.f("uq_orchestrator_task_step_task_id_step_key")
        ),
    )
    op.create_index(
        op.f("ix_orchestrator_task_step_project_id"),
        "orchestrator_task_step",
        ["project_id"],
        unique=False,
    )
    op.add_column("chat_turn", sa.Column("task_id", sa.Uuid(), nullable=True))
    op.drop_constraint(op.f("ck_chat_turn_trigger"), "chat_turn", type_="check")
    op.create_check_constraint(
        op.f("ck_chat_turn_trigger"),
        "chat_turn",
        "trigger IN ('message', 'research_finished', 'studio_finished', 'task_finished')",
    )
    op.create_check_constraint(
        op.f("ck_chat_turn_task_id"),
        "chat_turn",
        "(trigger = 'task_finished') = (task_id IS NOT NULL)",
    )
    op.drop_constraint(op.f("ck_proposal_effect"), "proposal", type_="check")
    op.create_check_constraint(
        op.f("ck_proposal_effect"),
        "proposal",
        "effect IN ('internal', 'publishes', 'spends', 'confirms')",
    )
    op.add_column("writing_run", sa.Column("origin_action_id", sa.Text(), nullable=True))
    op.create_unique_constraint(
        op.f("uq_writing_run_origin_action_id"), "writing_run", ["origin_action_id"]
    )


def downgrade() -> None:
    op.drop_constraint(op.f("uq_writing_run_origin_action_id"), "writing_run", type_="unique")
    op.drop_column("writing_run", "origin_action_id")
    op.execute("DELETE FROM proposal WHERE effect = 'confirms' OR run_kind = 'orchestrator-task'")
    op.drop_constraint(op.f("ck_proposal_effect"), "proposal", type_="check")
    op.create_check_constraint(
        op.f("ck_proposal_effect"), "proposal", "effect IN ('internal', 'publishes', 'spends')"
    )
    task_turns = "SELECT id FROM chat_turn WHERE trigger = 'task_finished'"
    op.execute(f"UPDATE chat_message SET turn_id = NULL WHERE turn_id IN ({task_turns})")
    op.execute(
        f"DELETE FROM run_event WHERE run_kind = 'orchestrator-chat' AND run_id IN ({task_turns})"
    )
    op.execute("DELETE FROM run_event WHERE run_kind = 'orchestrator-task'")
    op.execute("DELETE FROM chat_turn WHERE trigger = 'task_finished'")
    op.drop_constraint(op.f("ck_chat_turn_task_id"), "chat_turn", type_="check")
    op.drop_constraint(op.f("ck_chat_turn_trigger"), "chat_turn", type_="check")
    op.create_check_constraint(
        op.f("ck_chat_turn_trigger"),
        "chat_turn",
        "trigger IN ('message', 'research_finished', 'studio_finished')",
    )
    op.drop_column("chat_turn", "task_id")
    op.drop_index(op.f("ix_orchestrator_task_step_project_id"), table_name="orchestrator_task_step")
    op.drop_table("orchestrator_task_step")
    op.drop_index("ix_orchestrator_task_project_id_created_at", table_name="orchestrator_task")
    op.drop_index(op.f("ix_orchestrator_task_project_id"), table_name="orchestrator_task")
    op.drop_index(op.f("ix_orchestrator_task_chat_thread_id"), table_name="orchestrator_task")
    op.drop_table("orchestrator_task")
