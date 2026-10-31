"""Tests for what a post may be written with: an asset of the library and a research run.

They need PostgreSQL (see the root ``conftest.py``). They cover which asset and which research
run a post can use (``usable_asset``, ``succeeded_research_run``), what the writer reads of them,
the checks of a writing run's request and of its content item, and the runs a post keeps. How a
writing run moves is in ``test_writing_runs.py``, how its item is settled in
``test_content_items.py``.
"""

import uuid
from dataclasses import dataclass
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from openmarketer_core.db.content_items import (
    ContentItemStored,
    NewContentItem,
    store_proposed_content_item,
)
from openmarketer_core.db.media_assets import (
    ASSET_OF_FAILED_RUN,
    ASSET_REJECTED,
    ASSET_UNFINISHED,
    AssetNotUsable,
    MediaAssetNotFound,
    MediaAssetVersionNotFound,
    NewAssetVersion,
    accept_asset,
    add_version,
    asset_for_post,
    reject_asset,
    usable_asset,
)
from openmarketer_core.db.models import (
    AssetTechnique,
    MediaAssetStatus,
    ProductProfileRecord,
    Project,
    ResearchReportRecord,
    ResearchRunStatus,
    Workspace,
    WritingRunRecord,
)
from openmarketer_core.db.project_removal import delete_project
from openmarketer_core.db.research_runs import (
    ResearchRunNotFound,
    ResearchRunUsedByPost,
    delete_research_run,
    request_research_run,
)
from openmarketer_core.db.research_runs import mark_run_failed as mark_research_failed
from openmarketer_core.db.research_runs import mark_run_started as mark_research_started
from openmarketer_core.db.research_runs import mark_run_succeeded as mark_research_succeeded
from openmarketer_core.db.research_store import (
    ResearchRunNotUsable,
    StoredResearchReportInvalid,
    research_findings_for_post,
    save_research_report,
    succeeded_research_run,
)
from openmarketer_core.db.studio_runs import (
    StudioRunUsedByPost,
    delete_studio_run,
    request_studio_run,
)
from openmarketer_core.db.studio_runs import mark_run_failed as mark_studio_failed
from openmarketer_core.db.studio_runs import mark_run_needs_review as mark_studio_needs_review
from openmarketer_core.db.studio_runs import mark_run_started as mark_studio_started
from openmarketer_core.db.writing_runs import mark_run_failed as mark_writing_failed
from openmarketer_core.db.writing_runs import mark_run_started as mark_writing_started
from openmarketer_core.db.writing_runs import request_writing_run
from openmarketer_core.research.questions import ResearchRequest
from openmarketer_core.research.research_agent.rules import CheckedFinding, Report
from openmarketer_core.studio.asset_store import asset_key
from openmarketer_core.studio.platforms import Platform
from openmarketer_core.studio.requests import AssetRequest
from openmarketer_core.writing.brief import WritingBrief

PERSON = uuid.UUID("11111111-1111-4111-8111-111111111111")
ASSET_REQUEST = AssetRequest(
    goal="Announce offline sync",
    exact_copy=["Offline sync is here", "Memoria 2.0"],
    platform=Platform.INSTAGRAM_POST,
)
RESEARCH_REQUEST = ResearchRequest(questions=["How do note apps announce offline sync?"])


@dataclass(frozen=True)
class ProjectIds:
    workspace_id: uuid.UUID
    project_id: uuid.UUID

    def ids(self) -> dict:
        return {"workspace_id": self.workspace_id, "project_id": self.project_id}


@dataclass(frozen=True)
class Image:
    studio_run_id: uuid.UUID
    asset_id: uuid.UUID
    version_id: uuid.UUID


def new_project(session: Session) -> ProjectIds:
    workspace = Workspace(name="Acme")
    session.add(workspace)
    session.flush()
    project = Project(
        workspace_id=workspace.id, name="App", source_repo_url="https://example.com/a.git"
    )
    session.add(project)
    session.flush()
    return ProjectIds(workspace.id, project.id)


@pytest.fixture
def project(session) -> ProjectIds:
    return new_project(session)


def image(session: Session, project: ProjectIds, *, run_ends: str | None = "needs_review") -> Image:
    """A studio run with one asset of one version; the run ends as ``run_ends`` says."""
    run = request_studio_run(session, **project.ids(), request=ASSET_REQUEST, requested_by="person")
    scope = {**project.ids(), "run_id": run.id}
    mark_studio_started(session, **scope)
    version_id = uuid.uuid4()
    version = add_version(
        session,
        **scope,
        version_id=version_id,
        action_id=f"studio-run:{run.id}:1",
        version=NewAssetVersion(
            round=1,
            technique=AssetTechnique.GENERATED,
            storage_key=asset_key(**scope, version_id=version_id, mime="image/png"),
            mime="image/png",
            width=1080,
            height=1080,
            size_bytes=2048,
            cost_usd=Decimal("0.04"),
        ),
    )
    if run_ends == "needs_review":
        mark_studio_needs_review(
            session, **scope, final_version_id=version.id, cost_usd=Decimal("0.04")
        )
    elif run_ends == "failed":
        mark_studio_failed(session, **scope, error="the image model refused")
    return Image(run.id, version.asset_id, version.id)


def finding(statement: str, *, quotes: int = 1) -> CheckedFinding:
    return CheckedFinding(
        question_index=0,
        statement=statement,
        confidence=0.8,
        flagged=False,
        sources=[
            {
                "page_id": str(uuid.uuid4()),
                "url": "https://example.com/launch",
                "quotes": [f"quote {n}" for n in range(quotes)],
            }
        ],
    )


def research(
    session: Session,
    project: ProjectIds,
    *,
    ends: str | None = "succeeded",
    findings: tuple[CheckedFinding, ...] = (),
) -> uuid.UUID:
    """A research run of the project; it succeeds with ``findings``, fails, or is still queued."""
    run = request_research_run(
        session, **project.ids(), request=RESEARCH_REQUEST, requested_by="person"
    )
    scope = {**project.ids(), "run_id": run.id}
    if ends is None:
        return run.id
    mark_research_started(session, **scope)
    if ends == "failed":
        mark_research_failed(session, **scope, error="the search service was unreachable")
        return run.id
    report = save_research_report(
        session,
        **scope,
        report=Report(
            questions=list(RESEARCH_REQUEST.questions),
            findings=list(findings),
            notes=[],
            cost_usd=0.01,
            model="fake/model",
        ),
    )
    mark_research_succeeded(session, **scope, report_id=report.id)
    return run.id


def brief(**fields) -> WritingBrief:
    return WritingBrief.model_validate({"platform": "x", "goal": "introduce", **fields})


def running_writing_run(session: Session, project: ProjectIds, **fields) -> uuid.UUID:
    """A started writing run with its approved profile version: inserted as a draft, approved
    by UPDATE."""
    version = ProductProfileRecord(project_id=project.project_id, version=1, content={})
    session.add(version)
    session.flush()
    session.execute(
        text(
            "UPDATE product_profile SET status = 'approved', approved_by = :by,"
            " approved_at = now() WHERE id = :id"
        ),
        {"by": PERSON, "id": version.id},
    )
    run = request_writing_run(
        session, **project.ids(), brief=brief(**fields), requested_by="person"
    )
    mark_writing_started(session, **project.ids(), run_id=run.id, profile_version_id=version.id)
    return run.id


def item(**fields) -> NewContentItem:
    return NewContentItem(
        text="Offline sync is here.",
        plan={},
        check_report={},
        critic_report={},
        cost_usd=Decimal(0),
        **fields,
    )


def writing_runs(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(WritingRunRecord)) or 0


# ------------------------------------------------------------------------ the asset
def test_a_post_can_go_with_a_draft_asset_its_run_ended_with(session, project):
    made = image(session, project)

    asset = usable_asset(session, **project.ids(), asset_id=made.asset_id)

    assert asset.status is MediaAssetStatus.DRAFT
    assert asset.current_version_id == made.version_id


def test_the_writer_reads_what_the_assets_run_was_asked(session, project):
    made = image(session, project)

    asset = asset_for_post(session, **project.ids(), asset_id=made.asset_id)

    assert asset.id == made.asset_id
    assert asset.version_id == made.version_id
    assert asset.goal == "Announce offline sync"
    assert asset.exact_copy == ("Offline sync is here", "Memoria 2.0")
    assert asset.platform is Platform.INSTAGRAM_POST


def test_a_rejected_asset_cannot_go_with_a_post(session, project):
    made = image(session, project)
    reject_asset(session, **project.ids(), asset_id=made.asset_id, decided_by=PERSON)

    with pytest.raises(AssetNotUsable) as refused:
        asset_for_post(session, **project.ids(), asset_id=made.asset_id)
    assert refused.value.reason == ASSET_REJECTED


def test_an_asset_still_being_made_cannot_go_with_a_post(session, project):
    made = image(session, project, run_ends=None)

    with pytest.raises(AssetNotUsable) as refused:
        usable_asset(session, **project.ids(), asset_id=made.asset_id)
    assert refused.value.reason == ASSET_UNFINISHED


def test_a_draft_of_a_failed_run_cannot_go_with_a_post(session, project):
    made = image(session, project, run_ends="failed")

    with pytest.raises(AssetNotUsable) as refused:
        usable_asset(session, **project.ids(), asset_id=made.asset_id)
    assert refused.value.reason == ASSET_OF_FAILED_RUN


def test_an_asset_a_person_accepted_can_go_with_a_post_whatever_its_run_ended(session, project):
    made = image(session, project, run_ends="failed")
    accept_asset(session, **project.ids(), asset_id=made.asset_id, decided_by=PERSON)

    asset = usable_asset(session, **project.ids(), asset_id=made.asset_id)

    assert asset.status is MediaAssetStatus.ACCEPTED


def test_an_asset_of_another_workspace_is_missing_for_a_post(session, project):
    made = image(session, project)
    other = new_project(session)

    with pytest.raises(MediaAssetNotFound):
        asset_for_post(session, **other.ids(), asset_id=made.asset_id)


# --------------------------------------------------------------------- the research
def test_a_post_can_draw_on_a_succeeded_research_run(session, project):
    run_id = research(session, project)

    run = succeeded_research_run(session, **project.ids(), run_id=run_id)

    assert run.status is ResearchRunStatus.SUCCEEDED


@pytest.mark.parametrize("ends", [None, "failed"])
def test_a_research_run_that_has_not_succeeded_cannot_inform_a_post(session, project, ends):
    run_id = research(session, project, ends=ends)

    with pytest.raises(ResearchRunNotUsable):
        research_findings_for_post(
            session, **project.ids(), run_id=run_id, limit=5, quotes_per_source=2
        )


def test_the_findings_for_a_post_are_read_with_their_sources(session, project):
    run_id = research(session, project, findings=(finding("Apps lead with the benefit."),))

    found = research_findings_for_post(
        session, **project.ids(), run_id=run_id, limit=5, quotes_per_source=2
    )

    assert found.questions == tuple(RESEARCH_REQUEST.questions)
    [read] = found.findings
    assert read.statement == "Apps lead with the benefit."
    assert (read.confidence, read.flagged) == (0.8, False)
    assert [(source.url, source.quotes) for source in read.sources] == [
        ("https://example.com/launch", ("quote 0",))
    ]


def test_the_findings_for_a_post_are_bounded(session, project):
    many = tuple(finding(f"Finding {n}.", quotes=4) for n in range(4))
    run_id = research(session, project, findings=many)

    found = research_findings_for_post(
        session, **project.ids(), run_id=run_id, limit=2, quotes_per_source=1
    )

    assert [read.statement for read in found.findings] == ["Finding 0.", "Finding 1."]
    assert found.left_out == 2
    assert all(len(source.quotes) == 1 for read in found.findings for source in read.sources)


def test_a_stored_finding_that_no_longer_fits_is_refused(session, project):
    run_id = research(session, project, findings=(finding("Apps lead with the benefit."),))
    session.execute(
        text("UPDATE research_report SET findings = :findings WHERE project_id = :project"),
        {"findings": '[{"statement": "", "sources": "everywhere"}]', "project": project.project_id},
    )

    with pytest.raises(StoredResearchReportInvalid):
        research_findings_for_post(
            session, **project.ids(), run_id=run_id, limit=5, quotes_per_source=2
        )


def test_the_findings_of_another_workspaces_run_are_missing(session, project):
    run_id = research(session, project, findings=(finding("Apps lead with the benefit."),))
    other = new_project(session)

    with pytest.raises(ResearchRunNotFound):
        research_findings_for_post(
            session, **other.ids(), run_id=run_id, limit=5, quotes_per_source=2
        )


# ----------------------------------------------------------------------- the request
def test_a_writing_run_is_requested_with_a_usable_asset_and_succeeded_research(session, project):
    made = image(session, project)
    research_id = research(session, project)

    run = request_writing_run(
        session,
        **project.ids(),
        brief=brief(asset=str(made.asset_id), research_run_id=str(research_id)),
        requested_by="person",
    )

    assert run.id is not None


def test_a_writing_run_with_an_asset_that_cannot_go_with_a_post_is_not_requested(session, project):
    made = image(session, project)
    reject_asset(session, **project.ids(), asset_id=made.asset_id, decided_by=PERSON)

    with pytest.raises(AssetNotUsable):
        request_writing_run(
            session,
            **project.ids(),
            brief=brief(asset=str(made.asset_id)),
            requested_by="person",
        )
    assert writing_runs(session) == 0


def test_a_writing_run_with_an_asset_of_another_project_is_not_requested(session, project):
    made = image(session, new_project(session))

    with pytest.raises(MediaAssetNotFound):
        request_writing_run(
            session, **project.ids(), brief=brief(asset=str(made.asset_id)), requested_by="person"
        )


def test_a_writing_run_with_research_that_has_not_succeeded_is_not_requested(session, project):
    research_id = research(session, project, ends="failed")

    with pytest.raises(ResearchRunNotUsable):
        request_writing_run(
            session,
            **project.ids(),
            brief=brief(research_run_id=str(research_id)),
            requested_by="person",
        )
    assert writing_runs(session) == 0


def test_a_writing_run_with_research_of_another_project_is_not_requested(session, project):
    research_id = research(session, new_project(session))

    with pytest.raises(ResearchRunNotFound):
        request_writing_run(
            session,
            **project.ids(),
            brief=brief(research_run_id=str(research_id)),
            requested_by="person",
        )


# ---------------------------------------------------------------------- the content item
def test_an_item_records_the_image_and_research_it_was_written_with(session, project):
    made = image(session, project)
    research_id = research(session, project)
    run_id = running_writing_run(session, project)

    stored = store_proposed_content_item(
        session,
        **project.ids(),
        run_id=run_id,
        item=item(
            asset_id=made.asset_id,
            asset_version_id=made.version_id,
            research_run_id=research_id,
        ),
    )

    assert (stored.asset_id, stored.asset_version_id, stored.research_run_id) == (
        made.asset_id,
        made.version_id,
        research_id,
    )


def test_an_item_names_a_version_of_its_own_asset_only(session, project):
    made, other = image(session, project), image(session, project)
    run_id = running_writing_run(session, project)

    with pytest.raises(MediaAssetVersionNotFound):
        store_proposed_content_item(
            session,
            **project.ids(),
            run_id=run_id,
            item=item(asset_id=made.asset_id, asset_version_id=other.version_id),
        )


def test_an_item_names_an_asset_of_its_project_only(session, project):
    made = image(session, new_project(session))
    run_id = running_writing_run(session, project)

    with pytest.raises(MediaAssetNotFound):
        store_proposed_content_item(
            session,
            **project.ids(),
            run_id=run_id,
            item=item(asset_id=made.asset_id, asset_version_id=made.version_id),
        )


def test_an_item_names_research_of_its_project_only(session, project):
    research_id = research(session, new_project(session))
    run_id = running_writing_run(session, project)

    with pytest.raises(ResearchRunNotFound):
        store_proposed_content_item(
            session, **project.ids(), run_id=run_id, item=item(research_run_id=research_id)
        )


def test_an_item_names_an_asset_together_with_its_version():
    with pytest.raises(ValueError):
        item(asset_id=uuid.uuid4())


def test_storing_the_item_again_with_another_image_is_refused(session, project):
    made, other = image(session, project), image(session, project)
    run_id = running_writing_run(session, project)
    first = item(asset_id=made.asset_id, asset_version_id=made.version_id)
    store_proposed_content_item(session, **project.ids(), run_id=run_id, item=first)

    again = store_proposed_content_item(session, **project.ids(), run_id=run_id, item=first)
    assert again.asset_version_id == made.version_id
    with pytest.raises(ContentItemStored):
        store_proposed_content_item(
            session,
            **project.ids(),
            run_id=run_id,
            item=item(asset_id=other.asset_id, asset_version_id=other.version_id),
        )


def test_the_database_holds_an_items_asset_to_its_version(session, project):
    made = image(session, project)
    run_id = running_writing_run(session, project)
    stored = store_proposed_content_item(session, **project.ids(), run_id=run_id, item=item())

    with pytest.raises(IntegrityError), session.begin_nested():
        session.execute(
            text("UPDATE content_item SET asset_id = :asset WHERE id = :id"),
            {"asset": made.asset_id, "id": stored.id},
        )


def test_the_database_holds_an_items_asset_to_its_project(session, project):
    made = image(session, new_project(session))
    run_id = running_writing_run(session, project)
    stored = store_proposed_content_item(session, **project.ids(), run_id=run_id, item=item())

    with pytest.raises(IntegrityError), session.begin_nested():
        session.execute(
            text(
                "UPDATE content_item SET asset_id = :asset, asset_version_id = :version"
                " WHERE id = :id"
            ),
            {"asset": made.asset_id, "version": made.version_id, "id": stored.id},
        )


def test_the_database_holds_an_items_research_to_its_project(session, project):
    research_id = research(session, new_project(session))
    run_id = running_writing_run(session, project)
    stored = store_proposed_content_item(session, **project.ids(), run_id=run_id, item=item())

    with pytest.raises(IntegrityError), session.begin_nested():
        session.execute(
            text("UPDATE content_item SET research_run_id = :run WHERE id = :id"),
            {"run": research_id, "id": stored.id},
        )


# --------------------------------------------------------------- what a post keeps
def post_with(session: Session, project: ProjectIds) -> tuple[Image, uuid.UUID]:
    """A finished writing run whose item names an image and a research run."""
    made = image(session, project)
    research_id = research(session, project)
    run_id = running_writing_run(session, project)
    store_proposed_content_item(
        session,
        **project.ids(),
        run_id=run_id,
        item=item(
            asset_id=made.asset_id, asset_version_id=made.version_id, research_run_id=research_id
        ),
    )
    mark_writing_failed(session, **project.ids(), run_id=run_id, error="the critic refused it")
    return made, research_id


def test_a_studio_run_whose_asset_a_post_names_is_not_deleted(session, project):
    made, _ = post_with(session, project)

    with pytest.raises(StudioRunUsedByPost):
        delete_studio_run(session, **project.ids(), run_id=made.studio_run_id)


def test_a_research_run_a_post_drew_on_is_not_deleted(session, project):
    _, research_id = post_with(session, project)

    with pytest.raises(ResearchRunUsedByPost):
        delete_research_run(session, **project.ids(), run_id=research_id)


def test_a_project_is_deleted_with_its_posts_and_what_they_used(session, project):
    post_with(session, project)

    delete_project(session, **project.ids())

    assert session.get(Project, project.project_id) is None
    assert session.scalar(select(func.count()).select_from(ResearchReportRecord)) == 0
