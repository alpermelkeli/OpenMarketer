"""What the runtime's activities do for a writing run: the worker's side of the writer's graph.

The writer's graph and its rules are core's. ``WritingWork`` is what stands between them and
this worker's database and models, one method for each thing the agent runtime asks an agent
for:

- ``initial_state``: what a run starts from, which is the run's own brief;
- ``context``: what the nodes of one attempt are given: a chat model of the attempt's own,
  which answers the role ``writer``, the run's limits, the reading of the
  project's approved profile, which records the version the run writes from on the run, the
  readings of the asset and the research run the brief may name, each within the run's
  project and as it is now (a person may have rejected the asset since the run was asked), the
  reading of the project's connected account on the post's platform (never its token), and
  what a post is reckoned to cost (``config/publishing.yaml``, read when the worker starts);
- ``record_progress``: storing the events of each node as it ends (``progress.py`` in core);
- ``store_draft``: the handler of the action ``store_draft``: the draft stored as the run's
  content item, ``proposed``, with the asset, its version and the research run it was written
  with;
- ``publish_post``: the handler of the action ``publish_post``, which runs only once a person
  approved the post at the policy gate. A post whose payload names no account (the project had
  none connected when the run proposed it) is recorded as it would have been sent
  (``dry_run``) and sent nowhere. One that names an account is published from it: the account
  must still be the project's, the send is recorded as begun (core's ``begin_publishing``,
  which also refuses a text the workspace published already) and committed before anything is
  sent, and ``publishing.py`` publishes it once with the app and the token resolved at this
  attempt, and records it ``published``. Either way what is sent or recorded is what the person
  approved: the stored text and the stored image's version, and an image nobody rejected
  since;
- ``finish``: the small projection of the graph's result that the workflow's ``after`` settles.

``WordedSteps`` wraps the steps of the graph so that what a writing run fails with leaves the
activity as a failure with a type and a message that may be stored and shown.

They hold no rule of their own: what a post may say, how it is chosen and how a run and its
item change state are core's. Everything can run twice without doing its work twice: the
version is recorded once (the same again changes nothing), the draft is stored once for its run
(the same text again gives back the stored item, so the run is its key), a post recorded as a
dry run is settled once, and a post is published once (the item is the key of both: an attempt
that finds its send begun looks the post up on the platform before sending it again). The
actions' ids are not needed as keys: a run has one item, and an item is settled once.

The version the run writes from is recorded when ``prepare`` reads the approved profile, in
the same transaction, and not by a hook or by ``store_draft``: ``before`` runs before any
profile is read, ``after`` after the item that needs it was stored, and the draft the graph
stores does not name the version. Reading it again (an attempt that died before its checkpoint)
finds the same version, unless a newer one was approved meanwhile: the run then ends, worded,
rather than write from one version and record another.

No credential is kept here or put in the graph's state: the model of an attempt is made for the
run's workspace (``new_model``) the first time a node asks it something, so a step that calls no
model (the one that resumes once a person decided, days later) needs no key, and a key or a
model set from the dashboard is the one the next attempt uses. A key that is set nowhere, or a
credential that is stored and cannot be read (``WORKSPACE_FAILURES``), ends the run in the
words of its error, which name what to set and hold no value. The same holds for publishing:
the installation's app on the platform and the account's token are resolved by
``publishing.py`` at the attempt that sends, and handed to the platform's adapter only.

The proposal the gate held ``publish_post`` as is named on the item when the post is recorded as
a dry run or its send begins: the handler is given the action's id, and the proposal is read by
it within the run's project. The handler does not take the runtime's check of the approval as
enough: it records the post only when that proposal is the run's own ``publish_post``, approved,
with the very payload it was asked to record, and it never records a post with no proposal. A
post that was rejected or expired is settled by the workflow's ``after``, which knows no
action's id, so its item names no proposal.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from openmarketer_core.agent_runtime.actions import ActionOutcome
from openmarketer_core.agent_runtime.gate import ProposalStatus
from openmarketer_core.agent_runtime.model_input import DoesNotFit, read_strict_json
from openmarketer_core.agent_runtime.steps import GraphRunRef, StepOutcome
from openmarketer_core.credentials.resolution import CredentialUnreadable, ModelKeyNotSet
from openmarketer_core.db.content_items import (
    ContentItem,
    ContentItemNotFound,
    ContentItemSettled,
    ContentItemStored,
    NewContentItem,
    ProfileVersionNotChosen,
    ProposalNotApproved,
    ProposalOfAnotherRun,
    PublishingBegun,
    PublishingFromAnotherAccount,
    TextAlreadyPublished,
    begin_publishing,
    content_item,
    settle_content_item,
    store_proposed_content_item,
)
from openmarketer_core.db.media_assets import (
    AssetNotUsable,
    MediaAssetNotFound,
    MediaAssetVersionNotFound,
    asset_for_post,
    usable_asset,
)
from openmarketer_core.db.models import ContentItemStatus
from openmarketer_core.db.profile_versions import (
    ProfileVersionNotFound,
    latest_approved_profile,
    profile_version,
)
from openmarketer_core.db.projects import ProjectNotFound
from openmarketer_core.db.proposals import proposal_of_action
from openmarketer_core.db.research_runs import ResearchRunNotFound
from openmarketer_core.db.research_store import (
    ResearchReportNotFound,
    ResearchRunNotUsable,
    StoredResearchReportInvalid,
    research_findings_for_post,
)
from openmarketer_core.db.run_events import append_events
from openmarketer_core.db.session import DatabaseError, transaction
from openmarketer_core.db.social_accounts import connected_account
from openmarketer_core.db.writing_runs import (
    WritingRunFinished,
    WritingRunNotFound,
    WritingRunNotStarted,
    WritingRunToExecute,
    WritingRunVersionChosen,
    mark_run_started,
    run_to_execute,
    writing_run,
)
from openmarketer_core.graph_checkpoints import RunCheckpoints
from openmarketer_core.llm import ChatModel, LLMError
from openmarketer_core.llm_config import ConfigError
from openmarketer_core.run_events import RunEvent
from openmarketer_core.social_accounts.catalog import SocialPlatform, connectable_platform
from openmarketer_core.social_accounts.publishing import PublishingAccount
from openmarketer_core.social_accounts.publishing_costs import PublishingCosts
from openmarketer_core.studio.requests import StoredAssetRequestInvalid
from openmarketer_core.workspace_settings.model_settings import StoredModelChoiceInvalid
from openmarketer_core.writing.brief import StoredWritingBriefInvalid, WritingPlatform
from openmarketer_core.writing.failures import WRITING_FAILURES
from openmarketer_core.writing.workflow_contract import WRITING_RUN_KIND, WritingRunInput
from openmarketer_core.writing.writer_agent import rules
from openmarketer_core.writing.writer_agent.definitions import (
    PUBLISH_POST,
    ApprovedProfile,
    PublishPost,
    StoreDraft,
    WriterContext,
    WriterState,
    initial_state,
    result_of,
)
from openmarketer_core.writing.writer_agent.rules import (
    Limits,
    PostImage,
    PostImageNotUsable,
    ResearchFindings,
    ResearchNotUsable,
)
from openmarketer_worker.agent_runtime.activities import AgentGraph
from openmarketer_worker.agent_runtime.activity_support import CheckpointAccess, ModelOfTheAttempt
from openmarketer_worker.agent_runtime.vocabulary import DATABASE_UNAVAILABLE, MAX_ERROR_CHARACTERS
from openmarketer_worker.writer import steps
from openmarketer_worker.writer.activities import failure
from openmarketer_worker.writer.publishing import PostPublishing, account_gone, account_replaced

logger = logging.getLogger(__name__)

# What the credentials and choices of a run's workspace are refused with: the model provider's
# key is set nowhere, a stored value does not open, or a stored model choice breaks a rule. None
# passes by itself, and each is worded for whoever asked for the post.
WORKSPACE_FAILURES = (ModelKeyNotSet, CredentialUnreadable, StoredModelChoiceInvalid)

# Why the brief's asset or research run cannot be used, in the writer's words; an asset a post
# cannot go with says why itself (``AssetNotUsable.reason``).
NO_SUCH_ASSET = "the project has no such asset"
ASSET_REQUEST_UNREADABLE = "the request of its studio run does not fit its schema"
NO_SUCH_RESEARCH_RUN = "the project has no such run"
RESEARCH_WITHOUT_REPORT = "it has no report"
FINDINGS_UNREADABLE = "its stored findings do not fit their schema"

NEWER_VERSION_APPROVED = (
    "a newer version of the product profile was approved while the run was starting: "
    "request the post again"
)


class WritingWork:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        checkpoints: CheckpointAccess,
        *,
        new_model: Callable[[uuid.UUID], ChatModel],
        limits: Limits,
        publishing: PostPublishing,
        publishing_costs: PublishingCosts,
    ) -> None:
        """``new_model`` makes the chat model of one attempt for the run's workspace.

        It may raise one of ``WORKSPACE_FAILURES``, which ends the run. ``publishing`` publishes
        a post whose send began; ``publishing_costs`` is what a post is reckoned to cost.
        """
        self._sessions = sessions
        self._checkpoints = checkpoints
        self._new_model = new_model
        self._limits = limits
        self._publishing = publishing
        self._publishing_costs = publishing_costs

    def initial_state(self, run: GraphRunRef) -> WriterState:
        """What the graph of ``run`` starts from: the run and the brief it answers."""
        found = self._run_to_execute(run)
        return initial_state(
            WritingRunInput(
                workspace_id=found.workspace_id, project_id=found.project_id, run_id=found.run_id
            ),
            found.brief,
        )

    def context(self, run: GraphRunRef, checkpoints: RunCheckpoints) -> WriterContext:
        """What the nodes of one attempt at a step of ``run`` are given."""
        found = self._run_to_execute(run)
        return WriterContext(
            model=ModelOfTheAttempt(lambda: self._new_model(found.workspace_id)),
            limits=self._limits,
            approved_profile=lambda: self._approved_profile(found),
            post_image=lambda asset_id: self._post_image(found, asset_id),
            research_findings=lambda run_id: self._research_findings(found, run_id),
            publishing_account=lambda platform: self._publishing_account(found, platform),
            publishing_costs=self._publishing_costs,
        )

    def record_progress(self, run: GraphRunRef, events: Sequence[RunEvent]) -> None:
        """Store the events of one node of ``run``, in a short transaction of their own.

        The run is handed by its id alone, so its workspace and project are read from its row
        in the same transaction. A run that is not there records nothing. Whatever this raises
        is logged by the step and never fails it (``AgentSteps``).
        """
        with transaction(self._sessions) as session:
            found = run_to_execute(session, run.run_id)
            if found is None:
                logger.info("progress of writing run %s not recorded: no such run", run.run_id)
                return
            append_events(
                session,
                workspace_id=found.workspace_id,
                project_id=found.project_id,
                run_kind=WRITING_RUN_KIND,
                run_id=run.run_id,
                events=events,
            )

    def store_draft(self, payload: StoreDraft) -> Mapping[str, Any]:
        """Store the run's draft as its content item, ``proposed``; answers the item's id.

        The item takes the version the run recorded when it read the profile, and each claim
        the evidence of its features in that version: the payload carries none, as its paths
        would enter the workflow's history. A draft the run stored before is given back, so a
        repeat stores one; another draft for the same run is refused for good.
        """
        try:
            with transaction(self._sessions) as session:
                item = NewContentItem(
                    text=payload.text,
                    plan=payload.plan.model_dump(mode="json"),
                    check_report=payload.check_report.model_dump(mode="json"),
                    critic_report=payload.critic_report.model_dump(mode="json"),
                    cost_usd=Decimal(str(payload.cost_usd)),
                    plan_changes=list(payload.plan_changes),
                    alternatives=[shown.model_dump(mode="json") for shown in payload.alternatives],
                    claims=_claims_with_evidence(session, payload),
                    flagged_numbers=list(payload.flagged_numbers),
                    asset_id=payload.asset_id,
                    asset_version_id=payload.asset_version_id,
                    research_run_id=payload.research_run_id,
                )
                stored = store_proposed_content_item(
                    session,
                    workspace_id=payload.workspace_id,
                    project_id=payload.project_id,
                    run_id=payload.run_id,
                    item=item,
                )
        except (
            WritingRunNotFound,
            WritingRunNotStarted,
            WritingRunFinished,
            ProjectNotFound,
        ) as e:
            raise failure(steps.RUN_UNAVAILABLE, str(e)) from None
        except (
            ProfileVersionNotChosen,
            ProfileVersionNotFound,
            ContentItemStored,
            ProposalOfAnotherRun,
            MediaAssetNotFound,
            MediaAssetVersionNotFound,
            ResearchRunNotFound,
        ) as e:
            raise failure(steps.WRITING_FAILED, str(e)) from None
        except DatabaseError:
            logger.exception("the draft of writing run %s was not stored", payload.run_id)
            raise failure(DATABASE_UNAVAILABLE, steps.DRAFT_NOT_STORED) from None
        return {"content_item_id": str(stored.id)}

    def publish_post(self, payload: PublishPost, action_id: str) -> Mapping[str, Any]:
        """Publish the approved post from the account it names, or record it as a dry run.

        The post must be the run's own item with the text and the image's version the person
        approved, so what is sent is what was approved, and the item names the proposal the
        gate held ``action_id`` as: one of this project, approved, of this run's
        ``publish_post``, with this very payload. Without such a proposal the post is refused
        for good and nothing is recorded or sent. An image a person rejected since, or that a
        post cannot go with any more, is not sent with it: the post is refused for good.
        Answers ``{"content_item_id", "status"}``, the status ``published`` or ``dry_run``;
        doing it again publishes or records nothing more.
        """
        if payload.dry_run:
            settled = self._record_dry_run(payload, action_id)
        else:
            settled = self._publish(payload, action_id)
        return {"content_item_id": str(settled.id), "status": settled.status.value}

    def _record_dry_run(self, payload: PublishPost, action_id: str) -> ContentItem:
        """Record the approved post as it would have been sent (``dry_run``); sends nothing."""
        try:
            with transaction(self._sessions) as session:
                stored = _approved_item(session, payload)
                if stored.asset_id is not None:
                    _require_usable_image(session, payload, stored.asset_id, stored.platform)
                return settle_content_item(
                    session,
                    workspace_id=payload.workspace_id,
                    project_id=payload.project_id,
                    item_id=payload.content_item_id,
                    status=ContentItemStatus.DRY_RUN,
                    proposal_id=_approved_proposal(session, payload, action_id),
                )
        except _ITEM_GONE as e:
            raise failure(steps.RUN_UNAVAILABLE, str(e)) from None
        except (ProposalOfAnotherRun, ProposalNotApproved) as e:
            raise failure(steps.WRITING_FAILED, str(e)) from None
        except DatabaseError:
            logger.exception("the post of writing run %s was not recorded", payload.run_id)
            raise failure(DATABASE_UNAVAILABLE, steps.POST_NOT_RECORDED) from None

    def _publish(self, payload: PublishPost, action_id: str) -> ContentItem:
        """Publish the approved post from its account, once: the item recorded ``published``.

        The installation's app and the account's token are resolved at this attempt before the
        send is begun, so a credential that is missing or does not open leaves the post
        proposed. The send is then recorded as begun and committed before anything is sent.
        """
        platform = self._platform_of(payload)
        prepared = self._publishing.prepare(payload, platform)
        begun = self._begin_publishing(payload, action_id, platform)
        return self._publishing.complete(prepared, begun)

    def _platform_of(self, payload: PublishPost) -> SocialPlatform:
        """The platform the post's account is on: that of the post's own item."""
        try:
            with transaction(self._sessions) as session:
                stored = _approved_item(session, payload)
        except _ITEM_GONE as e:
            raise failure(steps.RUN_UNAVAILABLE, str(e)) from None
        platform = rules.social_platform(stored.platform)
        if platform is None:
            raise failure(
                steps.WRITING_FAILED,
                f"a post for {stored.platform.value} cannot be published from an account",
            )
        return platform

    def _begin_publishing(
        self, payload: PublishPost, action_id: str, platform: SocialPlatform
    ) -> PublishingBegun:
        """Record that the approved post's send from its account begins, and commit it.

        The account must still be the project's on the post's platform: one disconnected or
        replaced since the person approved the post is refused for good, and the post is never
        recorded as a dry run instead. The image is checked while the item is still proposed:
        once its send began, the platform may have it.
        """
        try:
            with transaction(self._sessions) as session:
                stored = _approved_item(session, payload)
                _require_the_approved_account(session, payload, platform)
                proposed = stored.status is ContentItemStatus.PROPOSED
                if proposed and stored.asset_id is not None:
                    _require_usable_image(session, payload, stored.asset_id, stored.platform)
                begun = begin_publishing(
                    session,
                    workspace_id=payload.workspace_id,
                    project_id=payload.project_id,
                    item_id=payload.content_item_id,
                    proposal_id=_approved_proposal(session, payload, action_id),
                    account=_account_of(payload),
                )
        except _ITEM_GONE as e:
            raise failure(steps.RUN_UNAVAILABLE, str(e)) from None
        except (
            ProposalOfAnotherRun,
            ProposalNotApproved,
            TextAlreadyPublished,
            PublishingFromAnotherAccount,
        ) as e:
            raise failure(steps.WRITING_FAILED, str(e)) from None
        except DatabaseError:
            logger.exception("the post of writing run %s was not begun", payload.run_id)
            raise failure(DATABASE_UNAVAILABLE, steps.POST_NOT_RECORDED) from None
        return begun

    def finish(self, run: GraphRunRef, final_state: Mapping[str, Any]) -> dict[str, Any]:
        """The small projection of the result the workflow's ``after`` settles the post with.

        Identifiers, a status, the cost as exact text and the action's error; never the post,
        the plan or the reports, which the content item holds. It stores nothing, so it is
        safe to repeat.
        """
        result = result_of(final_state)
        return {
            "status": result.status.value,
            "content_item_id": result.content_item_id,
            "profile_version_id": result.profile_version_id,
            "cost_usd": str(Decimal(str(result.cost_usd))),
            "rounds": result.rounds,
            "passed_critic": result.passed_critic,
            "error": result.action_error[:MAX_ERROR_CHARACTERS],
        }

    def _approved_profile(self, run: WritingRunToExecute) -> ApprovedProfile | None:
        """The project's latest approved profile, its version recorded on the run; or ``None``."""
        try:
            with transaction(self._sessions) as session:
                approved = latest_approved_profile(
                    session, workspace_id=run.workspace_id, project_id=run.project_id
                )
                if approved is None:
                    return None
                mark_run_started(
                    session,
                    workspace_id=run.workspace_id,
                    project_id=run.project_id,
                    run_id=run.run_id,
                    profile_version_id=approved.id,
                )
        except WritingRunVersionChosen:
            raise failure(steps.WRITING_FAILED, NEWER_VERSION_APPROVED) from None
        except (WritingRunNotFound, WritingRunFinished, ProjectNotFound) as e:
            raise failure(steps.RUN_UNAVAILABLE, str(e)) from None
        return ApprovedProfile(version_id=approved.id, profile=approved.profile)

    def _post_image(self, run: WritingRunToExecute, asset_id: uuid.UUID) -> PostImage:
        """The brief's asset as the writer reads it, if a post can go with it now."""
        try:
            with transaction(self._sessions) as session:
                return asset_for_post(
                    session,
                    workspace_id=run.workspace_id,
                    project_id=run.project_id,
                    asset_id=asset_id,
                    post_platform=run.brief.platform,
                )
        except MediaAssetNotFound:
            raise PostImageNotUsable(asset_id, NO_SUCH_ASSET) from None
        except AssetNotUsable as e:
            raise PostImageNotUsable(asset_id, e.reason) from None
        except StoredAssetRequestInvalid:
            raise PostImageNotUsable(asset_id, ASSET_REQUEST_UNREADABLE) from None

    def _research_findings(self, run: WritingRunToExecute, run_id: uuid.UUID) -> ResearchFindings:
        """The findings of the brief's research run the writer is shown, if it succeeded."""
        try:
            with transaction(self._sessions) as session:
                return research_findings_for_post(
                    session,
                    workspace_id=run.workspace_id,
                    project_id=run.project_id,
                    run_id=run_id,
                    limit=rules.MAX_POST_FINDINGS,
                    quotes_per_source=rules.MAX_SITE_QUOTES,
                )
        except ResearchRunNotFound:
            raise ResearchNotUsable(run_id, NO_SUCH_RESEARCH_RUN) from None
        except ResearchRunNotUsable as e:
            raise ResearchNotUsable(run_id, f"it is {e.status.value}, not succeeded") from None
        except ResearchReportNotFound:
            raise ResearchNotUsable(run_id, RESEARCH_WITHOUT_REPORT) from None
        except StoredResearchReportInvalid:
            raise ResearchNotUsable(run_id, FINDINGS_UNREADABLE) from None

    def _publishing_account(
        self, run: WritingRunToExecute, platform: SocialPlatform
    ) -> PublishingAccount | None:
        """The project's connected account on ``platform`` as a post names it, never its token."""
        with transaction(self._sessions) as session:
            account = connected_account(
                session, workspace_id=run.workspace_id, project_id=run.project_id, platform=platform
            )
        if account is None:
            return None
        return PublishingAccount(account_id=account.account_id, username=account.username)

    def _run_to_execute(self, run: GraphRunRef) -> WritingRunToExecute:
        with transaction(self._sessions) as session:
            found = run_to_execute(session, run.run_id)
        if found is None:
            raise failure(steps.RUN_UNAVAILABLE, f"there is no writing run {run.run_id}")
        return found


def _claims_with_evidence(session: Session, draft: StoreDraft) -> list[dict[str, Any]]:
    """The draft's claims, each with the evidence of its features in the run's profile version.

    Raises ``ProfileVersionNotChosen`` for a run that recorded no version, and what
    ``writing_run`` and ``profile_version`` raise for a run or version the project does not have.
    """
    run = writing_run(
        session, workspace_id=draft.workspace_id, project_id=draft.project_id, run_id=draft.run_id
    )
    if run.profile_version is None:
        raise ProfileVersionNotChosen(draft.run_id)
    version = profile_version(
        session,
        workspace_id=draft.workspace_id,
        project_id=draft.project_id,
        version=run.profile_version,
    )
    return rules.claims_with_evidence(
        [claim.model_dump(mode="json") for claim in draft.claims],
        rules.writing_context(version.profile),
    )


# The item or its run is not there, or the item was settled another way.
_ITEM_GONE = (
    ContentItemNotFound,
    ContentItemSettled,
    WritingRunNotFound,
    WritingRunFinished,
    ProjectNotFound,
)


def _approved_item(session: Session, post: PublishPost) -> ContentItem:
    """The run's item ``post`` names, refused for good unless it holds what was approved."""
    stored = content_item(
        session,
        workspace_id=post.workspace_id,
        project_id=post.project_id,
        item_id=post.content_item_id,
    )
    if (stored.run_id, stored.text, stored.asset_version_id) != (
        post.run_id,
        post.text,
        post.asset_version_id,
    ):
        raise failure(
            steps.WRITING_FAILED,
            f"the approved post is not content item {post.content_item_id} "
            f"of writing run {post.run_id} as it was stored",
        )
    return stored


def _account_of(post: PublishPost) -> PublishingAccount:
    """The account ``post`` names; ``prepare`` refused a post that names none."""
    if post.account_id is None or post.username is None:
        raise failure(steps.WRITING_FAILED, "the approved post names no account")
    return PublishingAccount(account_id=post.account_id, username=post.username)


def _require_the_approved_account(
    session: Session, post: PublishPost, platform: SocialPlatform
) -> None:
    """Refuse for good a post whose account is no longer the project's on ``platform``."""
    account = connected_account(
        session, workspace_id=post.workspace_id, project_id=post.project_id, platform=platform
    )
    connectable = connectable_platform(platform.value)
    if account is None:
        raise failure(steps.WRITING_FAILED, account_gone(connectable))
    if account.account_id != post.account_id:
        raise failure(steps.WRITING_FAILED, account_replaced(connectable))


def _approved_proposal(session: Session, post: PublishPost, action_id: str) -> uuid.UUID:
    """The proposal ``post`` was approved as, held by the gate as ``action_id``; or refused.

    It must be of the post's project, approved, of the post's writing run and of its
    ``publish_post``, and hold the very payload the handler was given: an approval of anything
    else records nothing.
    """
    proposal = proposal_of_action(
        session, workspace_id=post.workspace_id, project_id=post.project_id, action_id=action_id
    )
    if (
        proposal is None
        or proposal.status is not ProposalStatus.APPROVED
        or proposal.run_kind != WRITING_RUN_KIND
        or proposal.run_id != post.run_id
        or proposal.action_name != PUBLISH_POST
        or _payload_of(proposal.payload) != post
    ):
        raise failure(
            steps.WRITING_FAILED,
            f"the post of writing run {post.run_id} has no proposal a person approved as it is",
        )
    return proposal.id


def _payload_of(stored: Mapping[str, Any]) -> PublishPost | None:
    """The payload a proposal holds, as ``publish_post`` reads it; ``None`` if it is not one."""
    try:
        return read_strict_json(PublishPost, stored)
    except DoesNotFit:
        return None


def _require_usable_image(
    session: Session, post: PublishPost, asset_id: uuid.UUID, platform: WritingPlatform
) -> None:
    """Refuse for good to record ``post`` when its image cannot go with a post on ``platform``
    any more."""
    try:
        usable_asset(
            session,
            workspace_id=post.workspace_id,
            project_id=post.project_id,
            asset_id=asset_id,
            post_platform=platform,
        )
    except MediaAssetNotFound:
        reason = NO_SUCH_ASSET
    except AssetNotUsable as e:
        reason = e.reason
    else:
        return
    raise failure(steps.WRITING_FAILED, str(PostImageNotUsable(asset_id, reason)))


class WordedSteps:
    """The steps of the writer's graph, with a writing run's failures worded for its run."""

    def __init__(self, graph: AgentGraph) -> None:
        self._graph = graph

    def advance(
        self, run: GraphRunRef, checkpoints: RunCheckpoints, resume: ActionOutcome | None
    ) -> StepOutcome:
        """Take the step, as ``AgentGraph.advance`` says.

        A project with no approved profile, a brief that announces a feature that is not live,
        answers of a model that cannot be read, a draft nothing can be proposed of, a brief that
        no longer fits its schema, a model whose key is not set and a configuration that refuses
        the call end the run (``WRITING_FAILED``); only the model provider fails in ways that
        pass, an outage or a rate limit (``MODEL_UNAVAILABLE``, tried again). The message is
        the error's own, which names no value.
        """
        try:
            return self._graph.advance(run, checkpoints, resume)
        except (
            *WRITING_FAILURES,
            StoredWritingBriefInvalid,
            *WORKSPACE_FAILURES,
            ConfigError,
        ) as e:
            logger.info("writing run %s failed: %s", run.run_id, e)
            passing = isinstance(e, LLMError) and e.may_pass
            kind = steps.MODEL_UNAVAILABLE if passing else steps.WRITING_FAILED
            raise failure(kind, str(e)) from None
