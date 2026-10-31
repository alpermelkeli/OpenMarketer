"""What the writer's tests are made of: scripted models, the profile, and its runs in the database.

The writer and the critic are scripted per role (``ScriptedChat``), with answers built the way
the core tests of the writer's graph build them. ``WriterDatabase`` sets up a project with an
approved profile and a requested writing run, with an image of the project's Studio library and
a research run it may name, and reads back what a run left. Nothing here reaches a model or a
network, and no image is stored anywhere: an asset's version is a row only.
"""

import json
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from openmarketer_core.db.content_items import ContentItem, content_item
from openmarketer_core.db.media_assets import NewAssetVersion, add_version, reject_asset
from openmarketer_core.db.models import AssetTechnique, ProductProfileRecord, Project, Workspace
from openmarketer_core.db.profile_versions import approve_profile_version
from openmarketer_core.db.proposals import ActionProposal, decide_proposal, list_proposals
from openmarketer_core.db.research_runs import mark_run_started as mark_research_started
from openmarketer_core.db.research_runs import mark_run_succeeded as mark_research_succeeded
from openmarketer_core.db.research_runs import request_research_run
from openmarketer_core.db.research_store import save_research_report
from openmarketer_core.db.session import transaction
from openmarketer_core.db.studio_runs import mark_run_needs_review as mark_studio_needs_review
from openmarketer_core.db.studio_runs import mark_run_started as mark_studio_started
from openmarketer_core.db.studio_runs import request_studio_run
from openmarketer_core.db.writing_runs import (
    WritingRun,
    mark_run_started,
    request_writing_run,
    writing_run,
)
from openmarketer_core.llm import ChatReply
from openmarketer_core.profile import ProductProfile
from openmarketer_core.research.questions import ResearchRequest
from openmarketer_core.research.research_agent.rules import CheckedFinding, Report
from openmarketer_core.studio.asset_store import asset_key
from openmarketer_core.studio.platforms import Platform
from openmarketer_core.studio.requests import AssetRequest
from openmarketer_core.writing.brief import WritingBrief, WritingGoal, WritingPlatform
from openmarketer_core.writing.request import REQUESTED_BY_A_PERSON
from openmarketer_core.writing.workflow_contract import WritingRunInput
from openmarketer_core.writing.writer_agent import rules
from openmarketer_worker.writer.policy import WriterPolicy

PERSON = uuid.UUID("99999999-9999-4999-8999-999999999999")
POST = "Your notes keep up offline. Write anywhere."
# What the profile says that a run's history must never hold: it is in the graph's state only.
FEATURE_DESCRIPTION = "Notes sync when the device is back online."
BRAND_VOICE = "Plain and warm, never hyped."
PROFILE = {
    "product": {"name": "Acme Notes", "type": "b2b_saas", "languages": ["en"]},
    "features": [
        {
            "id": "offline-sync",
            "description": FEATURE_DESCRIPTION,
            "status": "live",
            "evidence": [{"file": "src/sync.ts", "lines": "10-40"}],
            "confidence": 0.9,
        }
    ],
    "brand": {"voice": BRAND_VOICE, "do_not_mention": ["BetaCorp"]},
}
BRIEF = WritingBrief(platform=WritingPlatform.X, goal=WritingGoal.INTRODUCE, variants=1)
# What the studio run of the post's image was asked, and the research a post may draw on.
IMAGE_REQUEST = AssetRequest(
    goal="Announce offline sync",
    exact_copy=["Offline sync is here"],
    platform=Platform.INSTAGRAM_POST,
)
RESEARCH_REQUEST = ResearchRequest(questions=["How do note apps announce offline sync?"])
FINDING = "Note apps announce offline sync with a short demo."
QUOTES = ("Most launches show a ten second demo.", "Users ask for offline mode first.")
# Short limits for a workflow on a real server: a step is tried twice, a second apart.
QUICK = WriterPolicy(
    attempt_timeout_seconds=60, heartbeat_timeout_seconds=10, max_attempts=2, retry_after_seconds=1
)


# ------------------------------------------------------------------------ the scripted models
class ScriptedChat:
    """Prepared answers per role, in order: a reply, or an exception to raise. Records the roles."""

    def __init__(self, **answers: list[ChatReply | Exception]):
        self.answers = {role: list(queue) for role, queue in answers.items()}
        self.calls: list[str] = []

    def chat(self, role, messages, *, tools=None, tool_choice=None) -> ChatReply:
        self.calls.append(role)
        answer = self.answers[role].pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def left(self) -> dict[str, int]:
        return {role: len(queue) for role, queue in self.answers.items() if queue}


def text(content: Any, *, cost_usd: float = 0.01) -> ChatReply:
    body = content if isinstance(content, str) else json.dumps(content)
    return ChatReply(
        message={"role": "assistant", "content": body}, model="vendor/m", cost_usd=cost_usd
    )


def a_plan() -> ChatReply:
    plan = {"angle": "Write anywhere.", "feature_ids": ["offline-sync"], "format": "statement"}
    return text(plan)


def a_draft(body: str = POST) -> ChatReply:
    variant = {
        "text": body,
        "angle": "Freedom from connectivity.",
        "language": "en",
        "claims": [{"statement": "Notes sync after offline use.", "feature_ids": ["offline-sync"]}],
    }
    return text({"variants": [variant]})


def a_review() -> ChatReply:
    review = {
        "variant": 0,
        "criteria": [
            {"criterion": criterion.value, "passed": True, "severity": "none", "note": ""}
            for criterion in rules.Criterion
        ],
        "risky_claim": False,
        "risky_note": "",
        "suggestions": [],
    }
    return text({"reviews": [review], "ranking": [0]})


def one_round_that_passes() -> ScriptedChat:
    """A plan, one variant and a review that passes it: the run proposes it after one round."""
    return ScriptedChat(writer=[a_plan(), a_draft()], critic=[a_review()])


# ------------------------------------------------------------------------------- the database
@dataclass(frozen=True)
class Image:
    """An asset of a project's Studio library, one version of it, and the studio run it is of."""

    studio_run_id: uuid.UUID
    asset_id: uuid.UUID
    version_id: uuid.UUID


@dataclass(frozen=True)
class RequestedPost:
    """A writing run the tests set up, the profile version it should write from, and the image
    and the research run its brief names (``None`` when it names none)."""

    run: WritingRunInput
    version_id: uuid.UUID | None
    image: Image | None = None
    research_run_id: uuid.UUID | None = None


@dataclass(frozen=True)
class WriterDatabase:
    sessions: sessionmaker[Session]

    def requested(
        self,
        *,
        approved: bool = True,
        started: bool = False,
        with_image: bool = False,
        with_research: bool = False,
    ) -> RequestedPost:
        """A queued writing run of a new project in a new workspace, whose profile is approved
        unless ``approved`` is false; marked started, as ``start_run`` does, if ``started``.
        Its brief names an image of the project's library if ``with_image``, and a research run
        of the project that succeeded if ``with_research``."""
        with transaction(self.sessions) as session:
            workspace = Workspace(name="Acme")
            session.add(workspace)
            session.flush()
            project = Project(
                workspace_id=workspace.id,
                name="Acme Notes",
                source_repo_url="https://example.com/acme/notes.git",
            )
            session.add(project)
            session.flush()
            scope = {"workspace_id": workspace.id, "project_id": project.id}
            version_id = None
            if approved:
                version_id = self._approved_version(session, project.id, scope)
            image = _image(session, scope) if with_image else None
            research_run_id = research_run(session, scope) if with_research else None
            brief = BRIEF.model_copy(
                update={
                    "asset": image.asset_id if image else None,
                    "research_run_id": research_run_id,
                }
            )
            run = request_writing_run(
                session,
                **scope,
                brief=brief,
                requested_by=REQUESTED_BY_A_PERSON,
            )
            if started:
                mark_run_started(session, **scope, run_id=run.id)
        return RequestedPost(
            run=WritingRunInput(workspace_id=workspace.id, project_id=project.id, run_id=run.id),
            version_id=version_id,
            image=image,
            research_run_id=research_run_id,
        )

    def another_image(self, run: WritingRunInput) -> Image:
        """Another image of the run's project's library."""
        with transaction(self.sessions) as session:
            return _image(session, _scope(run))

    def another_research_run(self, run: WritingRunInput, **changes: Any) -> uuid.UUID:
        """Another research run of the run's project (see ``research_run``)."""
        with transaction(self.sessions) as session:
            return research_run(session, _scope(run), **changes)

    def reject(self, run: WritingRunInput, image: Image) -> None:
        """A person rejects the image, as the dashboard lets them."""
        with transaction(self.sessions) as session:
            reject_asset(session, **_scope(run), asset_id=image.asset_id, decided_by=PERSON)

    def approve_another_version(self, run: WritingRunInput) -> uuid.UUID:
        """Approve a newer version of the run's project's profile; its id."""
        with transaction(self.sessions) as session:
            scope = {"workspace_id": run.workspace_id, "project_id": run.project_id}
            return self._approved_version(session, run.project_id, scope)

    def _approved_version(
        self, session: Session, project_id: uuid.UUID, scope: dict[str, uuid.UUID]
    ) -> uuid.UUID:
        number = 1 + len(
            session.scalars(
                select(ProductProfileRecord.id).where(ProductProfileRecord.project_id == project_id)
            ).all()
        )
        content = ProductProfile.model_validate(PROFILE).model_dump(mode="json")
        session.add(ProductProfileRecord(project_id=project_id, version=number, content=content))
        session.flush()
        return approve_profile_version(session, **scope, version=number, approved_by=PERSON).id

    def run(self, run: WritingRunInput) -> WritingRun:
        with transaction(self.sessions) as session:
            return writing_run(
                session,
                workspace_id=run.workspace_id,
                project_id=run.project_id,
                run_id=run.run_id,
            )

    def item(self, run: WritingRunInput) -> ContentItem:
        """The content item the run stored."""
        item_id = self.run(run).content_item_id
        assert item_id is not None, "the run stored no content item"
        with transaction(self.sessions) as session:
            return content_item(
                session, workspace_id=run.workspace_id, project_id=run.project_id, item_id=item_id
            )

    def proposals(self, run: WritingRunInput) -> list[ActionProposal]:
        with transaction(self.sessions) as session:
            return list(
                list_proposals(
                    session, workspace_id=run.workspace_id, project_id=run.project_id, limit=5
                ).items
            )

    def decide(self, run: WritingRunInput, proposal: ActionProposal, *, approved: bool) -> None:
        with transaction(self.sessions) as session:
            decide_proposal(
                session,
                workspace_id=run.workspace_id,
                project_id=run.project_id,
                proposal_id=proposal.id,
                approved=approved,
                decided_by=PERSON,
            )


def _scope(run: WritingRunInput) -> dict[str, Any]:
    return {"workspace_id": run.workspace_id, "project_id": run.project_id}


def _image(session: Session, scope: dict[str, Any]) -> Image:
    """A studio run that made one asset of one version and waits for a person's review."""
    studio_run = request_studio_run(
        session, **scope, request=IMAGE_REQUEST, requested_by=REQUESTED_BY_A_PERSON
    )
    of_run = {**scope, "run_id": studio_run.id}
    mark_studio_started(session, **of_run)
    version_id = uuid.uuid4()
    version = add_version(
        session,
        **of_run,
        version_id=version_id,
        action_id=f"studio-run:{studio_run.id}:1",
        version=NewAssetVersion(
            round=1,
            technique=AssetTechnique.GENERATED,
            storage_key=asset_key(**of_run, version_id=version_id, mime="image/png"),
            mime="image/png",
            width=1080,
            height=1080,
            size_bytes=2048,
            cost_usd=Decimal("0.04"),
        ),
    )
    mark_studio_needs_review(
        session, **of_run, final_version_id=version.id, cost_usd=Decimal("0.04")
    )
    return Image(studio_run.id, version.asset_id, version.id)


def research_run(
    session: Session,
    scope: dict[str, Any],
    *,
    succeeded: bool = True,
    findings: int = 1,
) -> uuid.UUID:
    """A research run of the project, still queued unless it ``succeeded`` with ``findings``
    findings, each resting on one page quoted twice."""
    run = request_research_run(
        session, **scope, request=RESEARCH_REQUEST, requested_by=REQUESTED_BY_A_PERSON
    )
    if not succeeded:
        return run.id
    of_run = {**scope, "run_id": run.id}
    mark_research_started(session, **of_run)
    report = save_research_report(
        session,
        **of_run,
        report=Report(
            questions=list(RESEARCH_REQUEST.questions),
            findings=[
                CheckedFinding(
                    question_index=0,
                    statement=f"{FINDING} ({n})" if n else FINDING,
                    confidence=0.8,
                    flagged=False,
                    sources=[
                        {
                            "page_id": str(uuid.uuid4()),
                            "url": "https://example.com/launch",
                            "quotes": list(QUOTES),
                        }
                    ],
                )
                for n in range(findings)
            ],
            notes=[],
            cost_usd=0.01,
            model="fake/model",
        ),
    )
    mark_research_succeeded(session, **of_run, report_id=report.id)
    return run.id
