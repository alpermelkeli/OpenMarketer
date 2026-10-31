"""Tests for the migration that adds writing runs and content items.

They need PostgreSQL (see the root ``conftest.py``) and run the migrations themselves, so
they do not use the ``engine`` fixture. The rows are written at the revision itself, through
the stores where a store writes nothing a later revision added, and the content item in SQL
of the revision's own columns; the downgrade is then run over them.
"""

import uuid

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from openmarketer_core.agent_runtime.actions import ActionEffect, ActionRequest
from openmarketer_core.agent_runtime.gate import RunScope
from openmarketer_core.agent_runtime.steps import GraphRunRef
from openmarketer_core.db.models import ProductProfileRecord, Project, Workspace
from openmarketer_core.db.proposals import propose_action
from openmarketer_core.db.writing_runs import mark_run_started, request_writing_run
from openmarketer_core.writing.brief import WritingBrief

BEFORE = "75fd91d8fe65"
ADDS_WRITING = "6afb465902a5"
REVIEWER = uuid.UUID("11111111-1111-4111-8111-111111111111")


@pytest.fixture(scope="module")
def database(alembic_config):
    engine = create_engine(alembic_config.get_main_option("sqlalchemy.url"))
    yield engine
    engine.dispose()


def tables(engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def proposal(session: Session, scope: RunScope, kind: str, run_id: uuid.UUID) -> uuid.UUID:
    return propose_action(
        session,
        scope=scope,
        run=GraphRunRef(kind=kind, run_id=run_id),
        workflow_id=f"{kind}-{run_id}",
        request=ActionRequest(
            action_id=f"{kind}:{run_id}:1", name="publish_post", payload={"run_id": str(run_id)}
        ),
        effect=ActionEffect.PUBLISHES,
        wait_seconds=60,
    ).id


def a_proposed_post(engine) -> None:
    """A writing run with its proposal, its item and the proposal's event, and a studio run's
    proposal beside it."""
    with Session(engine) as session, session.begin():
        workspace = Workspace(name="Acme")
        session.add(workspace)
        session.flush()
        project = Project(
            workspace_id=workspace.id, name="App", source_repo_url="https://example.com/a.git"
        )
        session.add(project)
        session.flush()
        version = ProductProfileRecord(project_id=project.id, version=1, content={})
        session.add(version)
        session.flush()
        session.execute(
            text(
                "UPDATE product_profile SET status = 'approved', approved_by = :by,"
                " approved_at = now() WHERE id = :id"
            ),
            {"by": REVIEWER, "id": version.id},
        )
        scope = RunScope(workspace_id=workspace.id, project_id=project.id)
        ids = {"workspace_id": workspace.id, "project_id": project.id}
        run = request_writing_run(
            session,
            **ids,
            brief=WritingBrief.model_validate({"platform": "x", "goal": "introduce"}),
            requested_by="person",
        )
        mark_run_started(session, **ids, run_id=run.id, profile_version_id=version.id)
        item_id = session.execute(
            text(
                "INSERT INTO content_item (project_id, run_id, platform, text, plan,"
                " plan_changes, alternatives, claims, check_report, critic_report,"
                " flagged_numbers, profile_version_id, proposal_id)"
                " VALUES (:project, :run, 'x', 'Hello.', '{}', '[]', '[]', '[]', '{}', '{}',"
                " '[]', :version, :proposal) RETURNING id"
            ),
            {
                "project": project.id,
                "run": run.id,
                "version": version.id,
                "proposal": proposal(session, scope, "writing-run", run.id),
            },
        ).scalar_one()
        session.execute(
            text("UPDATE writing_run SET content_item_id = :item WHERE id = :run"),
            {"item": item_id, "run": run.id},
        )
        proposal(session, scope, "studio-run", uuid.uuid4())


def count(engine, query: str) -> int:
    with engine.connect() as conn:
        return conn.execute(text(query)).scalar_one()


def test_upgrade_adds_the_two_tables(alembic_config, database):
    command.upgrade(alembic_config, ADDS_WRITING)

    assert {"writing_run", "content_item"} <= tables(database)


def test_downgrade_drops_the_tables_with_the_proposals_and_events_of_writing_runs(
    alembic_config, database
):
    command.upgrade(alembic_config, ADDS_WRITING)
    a_proposed_post(database)
    before = tables(database)

    command.downgrade(alembic_config, BEFORE)

    assert before - tables(database) == {"writing_run", "content_item"}
    assert count(database, "SELECT count(*) FROM proposal WHERE run_kind = 'writing-run'") == 0
    assert count(database, "SELECT count(*) FROM run_event WHERE run_kind = 'writing-run'") == 0
    assert count(database, "SELECT count(*) FROM proposal WHERE run_kind = 'studio-run'") == 1


def test_upgrade_can_be_repeated_after_a_downgrade(alembic_config, database):
    command.upgrade(alembic_config, ADDS_WRITING)

    assert {"writing_run", "content_item"} <= tables(database)
