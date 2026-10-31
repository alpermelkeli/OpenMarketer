"""Tests for the migration that lets a post name its image and its research.

They need PostgreSQL (see the root ``conftest.py``) and run the migrations themselves, so they
do not use the ``engine`` fixture. The rows are written at the revision itself, through the
stores, and the downgrade is then run over them.
"""

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from openmarketer_core.db.chat import append_answer, create_chat
from openmarketer_core.db.chat_turns import record_unstarted_follow_up
from openmarketer_core.db.models import Project, Workspace
from openmarketer_core.db.writing_runs import request_writing_run
from openmarketer_core.writing.brief import WritingBrief

BEFORE = "6afb465902a5"
LETS_A_POST_NAME_ITS_INPUTS = "2530b8dce085"


@pytest.fixture(scope="module")
def database(alembic_config):
    engine = create_engine(alembic_config.get_main_option("sqlalchemy.url"))
    yield engine
    engine.dispose()


def columns(engine, table: str) -> set[str]:
    return {column["name"] for column in inspect(engine).get_columns(table)}


def count(engine, query: str) -> int:
    with engine.connect() as conn:
        return conn.execute(text(query)).scalar_one()


def a_follow_up_on_a_writing_run(engine) -> None:
    """A chat that asked for a writing run, with a follow-up turn on it and that turn's answer."""
    with Session(engine) as session, session.begin():
        workspace = Workspace(name="Acme")
        session.add(workspace)
        session.flush()
        project = Project(
            workspace_id=workspace.id, name="App", source_repo_url="https://example.com/a.git"
        )
        session.add(project)
        session.flush()
        ids = {"workspace_id": workspace.id, "project_id": project.id}
        chat = create_chat(session, workspace_id=workspace.id, project_id=project.id)
        run = request_writing_run(
            session,
            **ids,
            brief=WritingBrief.model_validate({"platform": "x", "goal": "introduce"}),
            requested_by="orchestrator",
            chat_thread_id=chat.id,
        )
        turn = record_unstarted_follow_up(
            session, **ids, chat_id=chat.id, writing_run_id=run.id, error="It could not start."
        )
        assert turn is not None
        append_answer(session, **ids, chat_id=chat.id, turn_id=turn.id, answer="The post waits.")


def test_upgrade_adds_the_columns(alembic_config, database):
    command.upgrade(alembic_config, LETS_A_POST_NAME_ITS_INPUTS)

    assert {"asset_id", "asset_version_id", "research_run_id"} <= columns(database, "content_item")
    assert "writing_run_id" in columns(database, "chat_turn")


def test_downgrade_drops_the_follow_ups_on_writing_runs_and_keeps_their_messages(
    alembic_config, database
):
    command.upgrade(alembic_config, LETS_A_POST_NAME_ITS_INPUTS)
    a_follow_up_on_a_writing_run(database)

    command.downgrade(alembic_config, BEFORE)

    assert "asset_id" not in columns(database, "content_item")
    assert "writing_run_id" not in columns(database, "chat_turn")
    assert count(database, "SELECT count(*) FROM chat_turn") == 0
    assert (
        count(database, "SELECT count(*) FROM run_event WHERE run_kind = 'orchestrator-chat'") == 0
    )
    assert count(database, "SELECT count(*) FROM chat_message WHERE turn_id IS NULL") == 1


def test_upgrade_can_be_repeated_after_a_downgrade(alembic_config, database):
    command.upgrade(alembic_config, LETS_A_POST_NAME_ITS_INPUTS)

    assert "research_run_id" in columns(database, "content_item")
