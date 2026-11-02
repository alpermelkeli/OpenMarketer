"""Tests for the migration that lets a post name its image and its research.

They need PostgreSQL (see the root ``conftest.py``) and run the migrations themselves, so they
do not use the ``engine`` fixture. The rows are written at the revision itself, through the
stores where a store writes nothing a later revision added, and the writing run and its post in
SQL of the revision's own columns; the downgrade is then run over them.
"""

import uuid
from decimal import Decimal

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from openmarketer_core.db.media_assets import NewAssetVersion, add_version
from openmarketer_core.db.models import AssetTechnique, ProductProfileRecord, Project, Workspace
from openmarketer_core.db.research_runs import mark_run_started as mark_research_started
from openmarketer_core.db.research_runs import mark_run_succeeded as mark_research_succeeded
from openmarketer_core.db.research_runs import request_research_run
from openmarketer_core.db.research_store import save_research_report
from openmarketer_core.db.studio_runs import mark_run_needs_review, request_studio_run
from openmarketer_core.db.studio_runs import mark_run_started as mark_studio_started
from openmarketer_core.research.questions import ResearchRequest
from openmarketer_core.research.research_agent.rules import Report
from openmarketer_core.studio.asset_store import asset_key
from openmarketer_core.studio.platforms import Platform
from openmarketer_core.studio.requests import AssetRequest

BEFORE = "6afb465902a5"
LETS_A_POST_NAME_ITS_INPUTS = "2530b8dce085"
REVIEWER = uuid.UUID("11111111-1111-4111-8111-111111111111")


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


def a_post_with_its_image_and_research(engine) -> None:
    """A proposed post that names a studio run's image and a succeeded research run."""
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

        studio = request_studio_run(
            session,
            workspace_id=workspace.id,
            project_id=project.id,
            request=AssetRequest(goal="Announce", platform=Platform.INSTAGRAM_POST),
            requested_by="person",
        )
        mark_studio_started(session, **ids, run_id=studio.id)
        version_id = uuid.uuid4()
        version = add_version(
            session,
            **ids,
            run_id=studio.id,
            version_id=version_id,
            action_id=f"studio-run:{studio.id}:1",
            version=NewAssetVersion(
                round=1,
                technique=AssetTechnique.GENERATED,
                storage_key=asset_key(
                    **ids, run_id=studio.id, version_id=version_id, mime="image/png"
                ),
                mime="image/png",
                width=1080,
                height=1080,
                size_bytes=2048,
                cost_usd=Decimal("0.04"),
            ),
        )
        mark_run_needs_review(
            session, **ids, run_id=studio.id, final_version_id=version.id, cost_usd=Decimal(0)
        )

        research = request_research_run(
            session,
            workspace_id=workspace.id,
            project_id=project.id,
            request=ResearchRequest(questions=["How do apps announce sync?"]),
            requested_by="person",
        )
        mark_research_started(session, **ids, run_id=research.id)
        report = save_research_report(
            session,
            **ids,
            run_id=research.id,
            report=Report(questions=[], findings=[], notes=[], cost_usd=0.0, model="fake/model"),
        )
        mark_research_succeeded(session, **ids, run_id=research.id, report_id=report.id)

        profile = ProductProfileRecord(project_id=project.id, version=1, content={})
        session.add(profile)
        session.flush()
        session.execute(
            text(
                "UPDATE product_profile SET status = 'approved', approved_by = :by,"
                " approved_at = now() WHERE id = :id"
            ),
            {"by": REVIEWER, "id": profile.id},
        )
        run_id = session.execute(
            text(
                "INSERT INTO writing_run (project_id, status, requested_by, brief, platform,"
                " profile_version_id, started_at) VALUES (:project, 'running', 'person',"
                ' \'{"platform": "x", "goal": "introduce"}\', \'x\', :version, now())'
                " RETURNING id"
            ),
            {"project": project.id, "version": profile.id},
        ).scalar_one()
        item_id = session.execute(
            text(
                "INSERT INTO content_item (project_id, run_id, platform, text, plan,"
                " plan_changes, alternatives, claims, check_report, critic_report,"
                " flagged_numbers, profile_version_id, asset_id, asset_version_id,"
                " research_run_id) VALUES (:project, :run, 'x', 'Offline sync is here.', '{}',"
                " '[]', '[]', '[]', '{}', '{}', '[]', :version, :asset, :asset_version,"
                " :research) RETURNING id"
            ),
            {
                "project": project.id,
                "run": run_id,
                "version": profile.id,
                "asset": version.asset_id,
                "asset_version": version.id,
                "research": research.id,
            },
        ).scalar_one()
        session.execute(
            text("UPDATE writing_run SET content_item_id = :item WHERE id = :run"),
            {"item": item_id, "run": run_id},
        )


def test_upgrade_adds_the_columns(alembic_config, database):
    command.upgrade(alembic_config, LETS_A_POST_NAME_ITS_INPUTS)

    assert {"asset_id", "asset_version_id", "research_run_id"} <= columns(database, "content_item")


def test_downgrade_forgets_what_a_post_used_and_keeps_the_post(alembic_config, database):
    command.upgrade(alembic_config, LETS_A_POST_NAME_ITS_INPUTS)
    a_post_with_its_image_and_research(database)

    command.downgrade(alembic_config, BEFORE)

    assert not {"asset_id", "asset_version_id", "research_run_id"} & columns(
        database, "content_item"
    )
    assert count(database, "SELECT count(*) FROM content_item") == 1


def test_upgrade_can_be_repeated_after_a_downgrade(alembic_config, database):
    command.upgrade(alembic_config, LETS_A_POST_NAME_ITS_INPUTS)

    assert "research_run_id" in columns(database, "content_item")
