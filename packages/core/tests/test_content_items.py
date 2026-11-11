"""Tests for content items: the post a writing run proposed, and how it is settled.

They need PostgreSQL (see the root ``conftest.py``). How the run itself moves is covered by
``test_writing_runs.py``; here a run is only what an item is stored and settled under.
"""

import uuid
from dataclasses import dataclass, replace
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text, update
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
    ProposalNotApproved,
    ProposalOfAnotherRun,
    PublishedAsAnotherPost,
    PublishingFromAnotherAccount,
    PublishingNotBegun,
    TextAlreadyPublished,
    begin_publishing,
    content_item,
    list_content_items,
    record_published,
    record_publishing_failed,
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
from openmarketer_core.db.proposals import decide_proposal, propose_action
from openmarketer_core.db.writing_runs import (
    WritingRunFinished,
    WritingRunNotStarted,
    WritingRunUnsettled,
    mark_run_failed,
    mark_run_started,
    mark_run_succeeded,
    request_writing_run,
    writing_run,
)
from openmarketer_core.social_accounts.publishing import (
    PublishedPost,
    PublishingAccount,
    PublishingFailure,
    PublishingStage,
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


def proposal_of(
    session: Session,
    run: RunOfProject,
    *,
    kind: str = WRITING_RUN_KIND,
    approved: bool | None = None,
) -> uuid.UUID:
    """A proposal of ``run``'s ``publish_post``: pending, or decided as ``approved`` says."""
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
    if approved is not None:
        decide_proposal(
            session,
            workspace_id=run.workspace_id,
            project_id=run.project_id,
            proposal_id=proposal.id,
            approved=approved,
            decided_by=REVIEWER,
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
    assert item.profile_version == stored_run.profile_version == 1
    [listed] = list_content_items(session, **run.scope(), limit=5).items
    assert listed == item


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
    "status", [ContentItemStatus.REJECTED, ContentItemStatus.EXPIRED, ContentItemStatus.FAILED]
)
def test_a_proposed_item_is_settled_once(session, run, status):
    item = store(session, run)

    settled = settle(session, run, item.id, status)

    assert settled.status is status
    assert settled.settled_at is not None
    assert settle(session, run, item.id, status) == settled


def test_a_proposed_item_is_settled_as_a_dry_run_once_with_an_approved_proposal(session, run):
    item = store(session, run)
    proposal_id = proposal_of(session, run, approved=True)

    settled = settle(session, run, item.id, ContentItemStatus.DRY_RUN, proposal_id=proposal_id)

    assert settled.status is ContentItemStatus.DRY_RUN
    assert settled.settled_at is not None
    assert settle(session, run, item.id, ContentItemStatus.DRY_RUN) == settled


def test_an_item_is_not_settled_as_a_dry_run_without_a_proposal(session, run):
    item = store(session, run)

    with pytest.raises(ProposalNotApproved):
        settle(session, run, item.id, ContentItemStatus.DRY_RUN)
    assert content_item(session, **run.scope(), item_id=item.id).status is (
        ContentItemStatus.PROPOSED
    )


@pytest.mark.parametrize("approved", [None, False])
def test_an_item_is_not_settled_as_a_dry_run_with_a_proposal_nobody_approved(
    session, run, approved
):
    item = store(session, run)
    proposal_id = proposal_of(session, run, approved=approved)

    with pytest.raises(ProposalNotApproved):
        settle(session, run, item.id, ContentItemStatus.DRY_RUN, proposal_id=proposal_id)


def test_an_item_naming_an_approved_proposal_is_settled_as_a_dry_run_without_naming_it_again(
    session, run
):
    item = store(session, run, replace(ITEM, proposal_id=proposal_of(session, run, approved=True)))

    settled = settle(session, run, item.id, ContentItemStatus.DRY_RUN)

    assert settled.status is ContentItemStatus.DRY_RUN


def test_an_item_is_not_settled_as_a_dry_run_with_the_approved_proposal_of_another_run(
    session, run
):
    item = store(session, run)
    other = another_run_of(session, run.workspace_id, run.project_id)

    with pytest.raises(ProposalOfAnotherRun):
        settle(
            session,
            run,
            item.id,
            ContentItemStatus.DRY_RUN,
            proposal_id=proposal_of(session, other, approved=True),
        )


def test_a_settled_item_is_not_settled_another_way(session, run):
    item = store(session, run)
    settle(session, run, item.id, ContentItemStatus.REJECTED)

    with pytest.raises(ContentItemSettled):
        settle(
            session,
            run,
            item.id,
            ContentItemStatus.DRY_RUN,
            proposal_id=proposal_of(session, run, approved=True),
        )


def test_an_item_is_not_settled_back_to_proposed(session, run):
    item = store(session, run)

    with pytest.raises(ValueError):
        settle(session, run, item.id, ContentItemStatus.PROPOSED)


def test_settling_names_the_proposal_when_the_item_did_not(session, run):
    item = store(session, run)
    proposal_id = proposal_of(session, run, approved=True)

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


# ------------------------------------------------------------------------------ publishing
ACCOUNT = PublishingAccount(account_id="42", username="acme")
POST = PublishedPost(external_id="1850000000000000001", url="https://x.com/acme/status/1850")
POST_COST = Decimal("0.015")


def approved_item(session: Session, run: RunOfProject, item: NewContentItem = ITEM):
    stored = store(session, run, item)
    return stored, proposal_of(session, run, approved=True)


def begin(session, run, item_id, proposal_id, account=ACCOUNT):
    return begin_publishing(
        session, **run.scope(), item_id=item_id, proposal_id=proposal_id, account=account
    )


def published(session, run, item_id, post=POST):
    return record_published(session, **run.scope(), item_id=item_id, post=post, cost_usd=POST_COST)


def test_an_approved_item_starts_publishing_from_its_account(session, run):
    item, proposal = approved_item(session, run)

    begun = begin(session, run, item.id, proposal)

    assert begun.stage is PublishingStage.STARTED
    assert begun.item.status is ContentItemStatus.PUBLISHING
    assert (begun.item.published_account_id, begun.item.published_username) == ("42", "acme")
    assert begun.item.proposal_id == proposal
    assert begun.item.publishing_started_at is not None and begun.item.settled_at is None


def test_beginning_again_from_the_same_account_is_a_send_whose_outcome_is_unknown(session, run):
    item, proposal = approved_item(session, run)
    first = begin(session, run, item.id, proposal)

    again = begin(session, run, item.id, proposal)

    assert again.stage is PublishingStage.RESUMED
    assert again.item.publishing_started_at == first.item.publishing_started_at


def test_beginning_a_published_item_again_says_it_is_done(session, run):
    item, proposal = approved_item(session, run)
    begin(session, run, item.id, proposal)
    published(session, run, item.id)

    assert begin(session, run, item.id, proposal).stage is PublishingStage.DONE


def test_an_item_being_published_from_one_account_is_not_published_from_another(session, run):
    item, proposal = approved_item(session, run)
    begin(session, run, item.id, proposal)

    with pytest.raises(PublishingFromAnotherAccount):
        begin(session, run, item.id, proposal, account=PublishingAccount("43", "other"))


def test_an_item_is_not_published_without_a_proposal_a_person_approved(session, run):
    item = store(session, run)
    pending = proposal_of(session, run)

    with pytest.raises(ProposalNotApproved):
        begin(session, run, item.id, pending)

    assert content_item(session, **run.scope(), item_id=item.id).status is (
        ContentItemStatus.PROPOSED
    )


def test_an_item_settled_another_way_is_not_published(session, run):
    item, proposal = approved_item(session, run)
    settle(session, run, item.id, ContentItemStatus.DRY_RUN, proposal_id=proposal)

    with pytest.raises(ContentItemSettled):
        begin(session, run, item.id, proposal)


def test_a_published_item_keeps_the_platforms_post_and_its_estimate(session, run):
    item, proposal = approved_item(session, run)
    begin(session, run, item.id, proposal)

    done = published(session, run, item.id)

    assert done.status is ContentItemStatus.PUBLISHED
    assert (done.external_id, done.external_url) == (POST.external_id, POST.url)
    assert done.publishing_cost_usd == POST_COST
    assert done.published_at is not None and done.settled_at is not None


def test_recording_the_same_post_again_changes_nothing(session, run):
    item, proposal = approved_item(session, run)
    begin(session, run, item.id, proposal)
    first = published(session, run, item.id)

    assert published(session, run, item.id) == first


def test_an_item_published_as_one_post_is_not_recorded_as_another(session, run):
    item, proposal = approved_item(session, run)
    begin(session, run, item.id, proposal)
    published(session, run, item.id)

    with pytest.raises(PublishedAsAnotherPost):
        published(session, run, item.id, PublishedPost("1850000000000000002", "https://x.com/a/2"))


def test_a_post_recorded_for_one_item_is_not_recorded_for_another(session, run):
    item, proposal = approved_item(session, run)
    begin(session, run, item.id, proposal)
    published(session, run, item.id)
    other = another_run_of(session, run.workspace_id, run.project_id)
    other_item, other_proposal = approved_item(session, other, replace(ITEM, text="Another post."))
    begin(session, other, other_item.id, other_proposal)

    with pytest.raises(PublishedAsAnotherPost):
        published(session, other, other_item.id)


def test_an_item_whose_send_never_started_has_no_outcome_to_record(session, run):
    item, _ = approved_item(session, run)

    with pytest.raises(PublishingNotBegun):
        published(session, run, item.id)
    with pytest.raises(PublishingNotBegun):
        record_publishing_failed(
            session, **run.scope(), item_id=item.id, failure=PublishingFailure.TOO_LONG
        )


def test_an_item_the_platform_refused_fails_with_why_once(session, run):
    item, proposal = approved_item(session, run)
    begin(session, run, item.id, proposal)

    def failed(failure):
        return record_publishing_failed(session, **run.scope(), item_id=item.id, failure=failure)

    first = failed(PublishingFailure.ACCESS_REVOKED)

    assert first.status is ContentItemStatus.FAILED
    assert first.publishing_failure is PublishingFailure.ACCESS_REVOKED
    assert failed(PublishingFailure.ACCESS_REVOKED) == first
    with pytest.raises(ContentItemSettled):
        failed(PublishingFailure.DUPLICATE)
    with pytest.raises(ContentItemSettled):
        published(session, run, item.id)


def test_a_text_another_project_published_is_refused_from_any_account(session, run):
    item, proposal = approved_item(session, run)
    begin(session, run, item.id, proposal)
    elsewhere = another_project_run(session, run)
    same_words = replace(ITEM, text=f"  {ITEM.text.replace(' ', '\n ')}  ")
    other_item, other_proposal = approved_item(session, elsewhere, same_words)

    with pytest.raises(TextAlreadyPublished) as refused:
        begin(session, elsewhere, other_item.id, other_proposal, PublishingAccount("43", "other"))

    assert refused.value.other_item_id == item.id
    assert refused.value.other_project_id == run.project_id
    assert content_item(session, **elsewhere.scope(), item_id=other_item.id).status is (
        ContentItemStatus.PROPOSED
    )


def test_a_text_that_failed_to_publish_may_be_published_by_another_item(session, run):
    item, proposal = approved_item(session, run)
    begin(session, run, item.id, proposal)
    record_publishing_failed(
        session, **run.scope(), item_id=item.id, failure=PublishingFailure.REFUSED
    )
    other = another_run_of(session, run.workspace_id, run.project_id)
    other_item, other_proposal = approved_item(session, other)

    assert begin(session, other, other_item.id, other_proposal).stage is PublishingStage.STARTED


def test_the_same_text_in_another_workspace_is_not_refused(session, run):
    item, proposal = approved_item(session, run)
    begin(session, run, item.id, proposal)
    other = new_run(session)
    other_item, other_proposal = approved_item(session, other)

    assert begin(session, other, other_item.id, other_proposal).stage is PublishingStage.STARTED


def test_a_run_that_fails_while_its_post_is_being_published_leaves_it_publishing(session, run):
    item, proposal = approved_item(session, run)
    begin(session, run, item.id, proposal)

    mark_run_failed(session, **run.ids(), error="the worker stopped")

    assert content_item(session, **run.scope(), item_id=item.id).status is (
        ContentItemStatus.PUBLISHING
    )


def test_a_run_whose_post_is_being_published_has_not_succeeded(session, run):
    item, proposal = approved_item(session, run)
    begin(session, run, item.id, proposal)

    with pytest.raises(WritingRunUnsettled):
        mark_run_succeeded(session, **run.ids(), cost_usd=Decimal("0.1"))

    published(session, run, item.id)
    finished = mark_run_succeeded(session, **run.ids(), cost_usd=Decimal("0.1"))
    assert finished.status is WritingRunStatus.SUCCEEDED


def test_the_database_refuses_a_published_item_without_its_post(session, run):
    item, proposal = approved_item(session, run)
    begin(session, run, item.id, proposal)

    with pytest.raises(IntegrityError), session.begin_nested():
        session.execute(
            update(ContentItemRecord)
            .where(ContentItemRecord.id == item.id)
            .values(status=ContentItemStatus.PUBLISHED, settled_at=func.now())
        )


def another_project_run(session: Session, run: RunOfProject) -> RunOfProject:
    project = Project(
        workspace_id=run.workspace_id, name="Other", source_repo_url="https://example.com/b.git"
    )
    session.add(project)
    session.flush()
    return another_run_of(session, run.workspace_id, project.id)
