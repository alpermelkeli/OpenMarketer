"""Tests for content items: the post a writing run proposed, and how it is settled.

They need PostgreSQL (see the root ``conftest.py``). How the run itself moves is covered by
``test_writing_runs.py``; here a run is only what an item is stored and settled under.
"""

import uuid
from dataclasses import dataclass, replace
from decimal import Decimal

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from openmarketer_core.agent_runtime.actions import ActionEffect, ActionRequest
from openmarketer_core.agent_runtime.gate import RunScope
from openmarketer_core.agent_runtime.steps import GraphRunRef
from openmarketer_core.db.content_items import (
    ContentItemNotFound,
    ContentItemSettled,
    ContentItemStored,
    NewContentItem,
    ProfileVersionNotChosen,
    ProposalOfAnotherRun,
    content_item,
    list_content_items,
    settle_content_item,
    store_proposed_content_item,
)
from openmarketer_core.db.models import (
    ContentItemRecord,
    ContentItemStatus,
    ProductProfileRecord,
    Project,
    Workspace,
    WritingRunStatus,
)
from openmarketer_core.db.projects import ProjectNotFound
from openmarketer_core.db.proposals import propose_action
from openmarketer_core.db.writing_runs import (
    WritingRunFinished,
    WritingRunNotStarted,
    mark_run_failed,
    mark_run_started,
    request_writing_run,
    writing_run,
)
from openmarketer_core.writing.brief import WritingBrief, WritingPlatform
from openmarketer_core.writing.workflow_contract import WRITING_RUN_KIND

BRIEF = WritingBrief.model_validate({"platform": "x", "goal": "introduce"})
REVIEWER = uuid.UUID("11111111-1111-4111-8111-111111111111")
ITEM = NewContentItem(
    text="Memoria keeps your notes in sync, offline too.",
    plan={"angle": "offline first", "features": ["sync"], "format": "single"},
    plan_changes=["dropped a feature that is not live"],
    alternatives=[{"text": "Notes that sync.", "language": "en"}],
    claims=[{"text": "syncs offline", "features": ["sync"]}],
    check_report={"passed": True, "findings": []},
    critic_report={"passed": True, "ranking": [0]},
    flagged_numbers=["3x"],
    cost_usd=Decimal("0.021"),
)


@dataclass(frozen=True)
class RunOfProject:
    workspace_id: uuid.UUID
    project_id: uuid.UUID
    run_id: uuid.UUID

    def scope(self) -> dict:
        return {"workspace_id": self.workspace_id, "project_id": self.project_id}

    def ids(self) -> dict:
        return {**self.scope(), "run_id": self.run_id}


def new_run(session: Session, *, start: bool = True, choose_version: bool = True) -> RunOfProject:
    workspace = Workspace(name="Acme")
    session.add(workspace)
    session.flush()
    project = Project(
        workspace_id=workspace.id, name="App", source_repo_url="https://example.com/a.git"
    )
    session.add(project)
    session.flush()
    return another_run_of(
        session, workspace.id, project.id, start=start, choose_version=choose_version
    )


def another_run_of(
    session: Session,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    *,
    start: bool = True,
    choose_version: bool = True,
) -> RunOfProject:
    run = request_writing_run(
        session,
        workspace_id=workspace_id,
        project_id=project_id,
        brief=BRIEF,
        requested_by="person",
    )
    found = RunOfProject(workspace_id=workspace_id, project_id=project_id, run_id=run.id)
    if start:
        version_id = approved_version(session, project_id) if choose_version else None
        mark_run_started(session, **found.ids(), profile_version_id=version_id)
    return found


def approved_version(session: Session, project_id: uuid.UUID) -> uuid.UUID:
    """An approved version of the project's profile: inserted as a draft, approved by UPDATE."""
    number = 1 + len(
        session.scalars(
            select(ProductProfileRecord.id).where(ProductProfileRecord.project_id == project_id)
        ).all()
    )
    record = ProductProfileRecord(
        project_id=project_id, version=number, content={"product": {"name": "App"}}
    )
    session.add(record)
    session.flush()
    session.execute(
        text(
            "UPDATE product_profile SET status = 'approved', approved_by = :by,"
            " approved_at = now() WHERE id = :id"
        ),
        {"by": REVIEWER, "id": record.id},
    )
    return record.id


def proposal_of(session: Session, run: RunOfProject, *, kind: str = WRITING_RUN_KIND) -> uuid.UUID:
    ref = GraphRunRef(kind=kind, run_id=run.run_id)
    proposal = propose_action(
        session,
        scope=RunScope(workspace_id=run.workspace_id, project_id=run.project_id),
        run=ref,
        workflow_id=f"writing-{run.run_id}",
        request=ActionRequest(
            action_id=f"{kind}:{run.run_id}:{uuid.uuid4().hex}",
            name="publish_post",
            payload={"run_id": str(run.run_id)},
        ),
        effect=ActionEffect.PUBLISHES,
        wait_seconds=3600,
    )
    return proposal.id


def store(session: Session, run: RunOfProject, item: NewContentItem = ITEM):
    return store_proposed_content_item(session, **run.ids(), item=item)


def settle(session: Session, run: RunOfProject, item_id: uuid.UUID, status, **fields):
    return settle_content_item(session, **run.scope(), item_id=item_id, status=status, **fields)


@pytest.fixture
def run(session) -> RunOfProject:
    return new_run(session)


def test_a_proposed_item_keeps_the_record_of_how_it_was_written(session, run):
    item = store(session, run)

    assert item.status is ContentItemStatus.PROPOSED
    assert item.platform is WritingPlatform.X
    assert item.text == ITEM.text
    assert item.plan == ITEM.plan
    assert item.plan_changes == tuple(ITEM.plan_changes)
    assert item.alternatives == tuple(ITEM.alternatives)
    assert item.claims == tuple(ITEM.claims)
    assert (item.check_report, item.critic_report) == (ITEM.check_report, ITEM.critic_report)
    assert item.flagged_numbers == ("3x",)
    assert item.cost_usd == ITEM.cost_usd
    assert item.settled_at is None
    assert content_item(session, **run.scope(), item_id=item.id) == item


def test_the_item_takes_the_runs_profile_version_and_becomes_its_item(session, run):
    item = store(session, run)

    stored_run = writing_run(session, **run.ids())
    assert stored_run.content_item_id == item.id
    assert item.profile_version_id == stored_run.profile_version_id


def test_storing_the_same_item_again_gives_back_the_stored_one(session, run):
    first = store(session, run)

    assert store(session, run) == first


def test_a_run_proposes_one_post(session, run):
    store(session, run)

    with pytest.raises(ContentItemStored):
        store(session, run, replace(ITEM, text="Another post."))


def test_an_item_needs_the_runs_profile_version(session):
    run = new_run(session, choose_version=False)

    with pytest.raises(ProfileVersionNotChosen):
        store(session, run)


def test_a_queued_run_stores_no_item(session):
    run = new_run(session, start=False)

    with pytest.raises(WritingRunNotStarted):
        store(session, run)


def test_a_finished_run_stores_no_item(session, run):
    mark_run_failed(session, **run.ids(), error="no")

    with pytest.raises(WritingRunFinished):
        store(session, run)


def test_an_item_names_a_proposal_of_its_own_run(session, run):
    proposal_id = proposal_of(session, run)

    item = store(session, run, replace(ITEM, proposal_id=proposal_id))

    assert item.proposal_id == proposal_id


def test_a_proposal_of_another_run_is_refused(session, run):
    other = another_run_of(session, run.workspace_id, run.project_id)
    proposal_id = proposal_of(session, other)

    with pytest.raises(ProposalOfAnotherRun):
        store(session, run, replace(ITEM, proposal_id=proposal_id))


def test_a_proposal_of_another_kind_of_run_is_refused(session, run):
    proposal_id = proposal_of(session, run, kind="studio-run")

    with pytest.raises(ProposalOfAnotherRun):
        store(session, run, replace(ITEM, proposal_id=proposal_id))


@pytest.mark.parametrize(
    "status",
    [
        ContentItemStatus.DRY_RUN,
        ContentItemStatus.REJECTED,
        ContentItemStatus.EXPIRED,
        ContentItemStatus.FAILED,
    ],
)
def test_a_proposed_item_is_settled_once(session, run, status):
    item = store(session, run)

    settled = settle(session, run, item.id, status)

    assert settled.status is status
    assert settled.settled_at is not None
    assert settle(session, run, item.id, status) == settled


def test_a_settled_item_is_not_settled_another_way(session, run):
    item = store(session, run)
    settle(session, run, item.id, ContentItemStatus.REJECTED)

    with pytest.raises(ContentItemSettled):
        settle(session, run, item.id, ContentItemStatus.DRY_RUN)


def test_an_item_is_not_settled_back_to_proposed(session, run):
    item = store(session, run)

    with pytest.raises(ValueError):
        settle(session, run, item.id, ContentItemStatus.PROPOSED)


def test_settling_names_the_proposal_when_the_item_did_not(session, run):
    item = store(session, run)
    proposal_id = proposal_of(session, run)

    settled = settle(session, run, item.id, ContentItemStatus.DRY_RUN, proposal_id=proposal_id)

    assert settled.proposal_id == proposal_id


def test_settling_with_another_proposal_than_the_items_is_refused(session, run):
    item = store(session, run, replace(ITEM, proposal_id=proposal_of(session, run)))

    with pytest.raises(ProposalOfAnotherRun):
        settle(
            session, run, item.id, ContentItemStatus.DRY_RUN, proposal_id=proposal_of(session, run)
        )


def test_the_item_of_a_finished_run_is_not_changed(session, run):
    item = store(session, run)
    mark_run_failed(session, **run.ids(), error="no")

    with pytest.raises(WritingRunFinished):
        settle(session, run, item.id, ContentItemStatus.FAILED)


def test_an_item_of_another_workspace_is_missing(session, run):
    item = store(session, run)
    elsewhere = new_run(session)

    with pytest.raises(ContentItemNotFound):
        content_item(
            session,
            workspace_id=elsewhere.workspace_id,
            project_id=run.project_id,
            item_id=item.id,
        )
    with pytest.raises(ContentItemNotFound):
        settle_content_item(
            session,
            workspace_id=elsewhere.workspace_id,
            project_id=run.project_id,
            item_id=item.id,
            status=ContentItemStatus.DRY_RUN,
        )


def test_a_projects_items_are_listed_newest_first_one_page_at_a_time(session, run):
    runs = [run] + [another_run_of(session, run.workspace_id, run.project_id) for _ in range(2)]
    items = [store(session, each) for each in runs]
    newest_first = sorted(items, key=lambda i: (i.created_at, i.id), reverse=True)

    first = list_content_items(session, **run.scope(), limit=2)
    rest = list_content_items(session, **run.scope(), limit=2, before=first.next_before)

    assert [i.id for i in first.items + rest.items] == [i.id for i in newest_first]
    assert rest.next_before is None


def test_the_items_of_a_project_the_workspace_does_not_have_are_not_listed(session, run):
    with pytest.raises(ProjectNotFound):
        list_content_items(
            session,
            workspace_id=new_run(session).workspace_id,
            project_id=run.project_id,
            limit=10,
        )


def test_the_database_refuses_a_settled_item_without_its_time(session, run):
    item = store(session, run)

    with pytest.raises(IntegrityError), session.begin_nested():
        session.execute(
            update(ContentItemRecord)
            .where(ContentItemRecord.id == item.id)
            .values(status=ContentItemStatus.DRY_RUN)
        )


def test_the_database_refuses_a_second_item_of_a_run(session, run):
    item = store(session, run)

    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            ContentItemRecord(
                project_id=run.project_id,
                run_id=run.run_id,
                platform="x",
                text="Another post.",
                plan={},
                plan_changes=[],
                alternatives=[],
                claims=[],
                check_report={},
                critic_report={},
                flagged_numbers=[],
                profile_version_id=item.profile_version_id,
            )
        )
        session.flush()


def test_the_database_refuses_a_profile_version_of_another_project(session, run):
    item = store(session, run)
    other = new_run(session)
    elsewhere = writing_run(session, **other.ids()).profile_version_id

    with pytest.raises(IntegrityError), session.begin_nested():
        session.execute(
            update(ContentItemRecord)
            .where(ContentItemRecord.id == item.id)
            .values(profile_version_id=elsewhere)
        )


def test_a_run_that_failed_after_proposing_keeps_its_item(session, run):
    item = store(session, run)

    failed = mark_run_failed(session, **run.ids(), error="no")

    assert failed.status is WritingRunStatus.FAILED
    assert failed.content_item_id == item.id
