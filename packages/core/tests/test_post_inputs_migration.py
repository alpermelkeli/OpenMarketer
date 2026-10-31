"""Tests for the migration that lets a post name its image and its research.

They need PostgreSQL (see the root ``conftest.py``) and run the migrations themselves, so they
do not use the ``engine`` fixture. The rows are written at the revision itself, through the
stores, and the downgrade is then run over them.
"""

import uuid
from decimal import Decimal

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from openmarketer_core.db.content_items import NewContentItem, store_proposed_content_item
from openmarketer_core.db.media_assets import NewAssetVersion, add_version
from openmarketer_core.db.models import AssetTechnique, ProductProfileRecord, Project, Workspace
from openmarketer_core.db.research_runs import mark_run_started as mark_research_started
from openmarketer_core.db.research_runs import mark_run_succeeded as mark_research_succeeded
from openmarketer_core.db.research_runs import request_research_run
from openmarketer_core.db.research_store import save_research_report
from openmarketer_core.db.studio_runs import mark_run_needs_review, request_studio_run
from openmarketer_core.db.studio_runs import mark_run_started as mark_studio_started
from openmarketer_core.db.writing_runs import mark_run_started as mark_writing_started
from openmarketer_core.db.writing_runs import request_writing_run
from openmarketer_core.research.questions import ResearchRequest
from openmarketer_core.research.research_agent.rules import Report
from openmarketer_core.studio.asset_store import asset_key
from openmarketer_core.studio.platforms import Platform
from openmarketer_core.studio.requests import AssetRequest
from openmarketer_core.writing.brief import WritingBrief

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
        run = request_writing_run(
            session,
            **ids,
            brief=WritingBrief.model_validate({"platform": "x", "goal": "introduce"}),
            requested_by="person",
        )
        mark_writing_started(session, **ids, run_id=run.id, profile_version_id=profile.id)
        store_proposed_content_item(
            session,
            **ids,
            run_id=run.id,
            item=NewContentItem(
                text="Offline sync is here.",
                plan={},
                check_report={},
                critic_report={},
                cost_usd=Decimal(0),
                asset_id=version.asset_id,
                asset_version_id=version.id,
                research_run_id=research.id,
            ),
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
