"""What the task's tests are made of: the orchestrator scripted, and tasks in the database.

The role ``orchestrator`` answers from a script (``ScriptedOrchestrator``), with plans and
reviews built as core's tests of the task's graph build them. ``TaskDatabase`` sets up a project
with an approved profile and a chat, and a task the chat asked for, and reads back what a task
left. Nothing here reaches a model or a network.
"""

import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from openmarketer_core.db.chat import create_chat
from openmarketer_core.db.models import ProductProfileRecord, Project, Workspace
from openmarketer_core.db.profile_versions import approve_profile_version
from openmarketer_core.db.proposals import ActionProposal, decide_proposal, list_proposals
from openmarketer_core.db.run_events import events_of_chat
from openmarketer_core.db.session import transaction
from openmarketer_core.db.tasks import OrchestratorTask, orchestrator_task, request_task_run
from openmarketer_core.llm import ChatReply
from openmarketer_core.orchestrator.task.plan import TaskRequest
from openmarketer_core.orchestrator.task.request import REQUESTED_BY_THE_CHAT
from openmarketer_core.orchestrator.task.workflow_contract import TaskRunInput
from openmarketer_core.profile import ProductProfile
from openmarketer_worker.orchestrator_task.policy import TaskPolicy

PERSON = uuid.UUID("88888888-8888-4888-8888-888888888888")
# What the profile says, and what the chat understood, that a task's history must never hold.
FEATURE_DESCRIPTION = "Notes sync when the device is back online."
BRAND_VOICE = "Plain and warm, never hyped."
UNDERSTANDING = "The operator wants one post on X announcing offline sync."
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
POST_REQUEST = "Write a post on X announcing offline sync."
POST_WITH_IMAGE = (
    'Write a post on X announcing offline sync, with an image that says "Write anywhere".'
)
COPY = "Write anywhere"
# Short limits for a workflow on a real server: a step is tried twice, a second apart.
QUICK = TaskPolicy(
    attempt_timeout_seconds=60, heartbeat_timeout_seconds=10, max_attempts=2, retry_after_seconds=1
)


# ------------------------------------------------------------------------ the scripted model
class ScriptedOrchestrator:
    """Prepared answers of the role ``orchestrator``, in order: a reply, or an error to raise."""

    def __init__(self, *answers: ChatReply | Exception):
        self.answers = list(answers)
        self.calls: list[str] = []

    def chat(self, role, messages, *, tools=None, tool_choice=None) -> ChatReply:
        self.calls.append(role)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def text(content: Any, *, cost_usd: float = 0.01) -> ChatReply:
    return ChatReply(
        message={"role": "assistant", "content": json.dumps(content)},
        model="vendor/m",
        cost_usd=cost_usd,
    )


def scope(**changes: Any) -> dict[str, Any]:
    return {"max_steps": 4, "max_plan_changes": 2, "deadline_days": 7, "max_cost_usd": 1, **changes}


def writing_step(step_id: str = "post", **fields: Any) -> dict[str, Any]:
    request = {"platform": "x", "goal": "announce", "feature": "offline-sync", "variants": 1}
    return {"id": step_id, "agent": "writing", "request": request, **fields}


def studio_step(step_id: str = "image") -> dict[str, Any]:
    request = {"goal": "Notes syncing on a plane.", "exact_copy": [COPY], "platform": "x_post"}
    return {"id": step_id, "agent": "studio", "request": request}


def a_plan(*steps: dict[str, Any]) -> ChatReply:
    return text({"goal": "Announce offline sync.", "steps": list(steps), "scope": scope()})


def a_post_plan() -> ChatReply:
    """A post without an image: the writing step only, which needs no confirmation."""
    return a_plan(writing_step())


def a_post_with_an_image_plan() -> ChatReply:
    """The studio step first, then the post that goes with its image: confirmed first."""
    return a_plan(studio_step(), writing_step(depends_on=["image"], asset_from_step="image"))


def a_review(decision: str = "finish") -> ChatReply:
    return text({"decision": decision, "reason": "Done.", "plan": None})


# ------------------------------------------------------------------------------- the database
@dataclass(frozen=True)
class RequestedTask:
    run: TaskRunInput
    chat_id: uuid.UUID | None


@dataclass(frozen=True)
class TaskDatabase:
    sessions: sessionmaker[Session]

    def requested(
        self, request: str = POST_REQUEST, *, from_a_chat: bool = True, approved: bool = True
    ) -> RequestedTask:
        """A queued task of a new project in a new workspace, whose profile is approved unless
        ``approved`` is false, asked for by a chat of the project unless ``from_a_chat`` is
        false."""
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
            ids = {"workspace_id": workspace.id, "project_id": project.id}
            if approved:
                self._approve(session, ids)
            chat_id = (
                create_chat(session, workspace_id=workspace.id, project_id=project.id).id
                if from_a_chat
                else None
            )
            task = request_task_run(
                session,
                **ids,
                request=TaskRequest(request=request, understanding=UNDERSTANDING),
                requested_by=REQUESTED_BY_THE_CHAT,
                chat_thread_id=chat_id,
            )
        return RequestedTask(TaskRunInput(workspace.id, project.id, task.id), chat_id)

    def _approve(self, session: Session, ids: dict[str, uuid.UUID]) -> None:
        number = 1 + len(
            session.scalars(
                select(ProductProfileRecord.id).where(
                    ProductProfileRecord.project_id == ids["project_id"]
                )
            ).all()
        )
        content = ProductProfile.model_validate(PROFILE).model_dump(mode="json")
        session.add(
            ProductProfileRecord(project_id=ids["project_id"], version=number, content=content)
        )
        session.flush()
        approve_profile_version(session, **ids, version=number, approved_by=PERSON)

    def task(self, run: TaskRunInput) -> OrchestratorTask:
        with transaction(self.sessions) as session:
            return orchestrator_task(
                session,
                workspace_id=run.workspace_id,
                project_id=run.project_id,
                task_id=run.run_id,
            )

    def proposals(self, run: TaskRunInput, action_name: str) -> list[ActionProposal]:
        """The project's proposals of ``action_name``, newest first."""
        with transaction(self.sessions) as session:
            listed = list_proposals(
                session, workspace_id=run.workspace_id, project_id=run.project_id, limit=20
            ).items
        return [proposal for proposal in listed if proposal.action_name == action_name]

    def decide(self, run: TaskRunInput, proposal: ActionProposal, *, approved: bool) -> None:
        with transaction(self.sessions) as session:
            decide_proposal(
                session,
                workspace_id=run.workspace_id,
                project_id=run.project_id,
                proposal_id=proposal.id,
                approved=approved,
                decided_by=PERSON,
            )

    def event_kinds(self, asked: RequestedTask) -> list[str]:
        """The kinds of the events the task's chat is shown, of the task's own, in order."""
        assert asked.chat_id is not None
        with transaction(self.sessions) as session:
            events = events_of_chat(
                session,
                workspace_id=asked.run.workspace_id,
                project_id=asked.run.project_id,
                chat_id=asked.chat_id,
                after=None,
                limit=100,
            )
        return [event.kind for event in events if event.run_id == asked.run.run_id]
