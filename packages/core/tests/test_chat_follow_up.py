"""Tests for following up in a chat on a run it started, once the run has ended.

They need PostgreSQL (see the root ``conftest.py``). The use case commits its own short
transactions, so rows are committed here too. The model is scripted (no network).
"""

import json
import uuid
from decimal import Decimal
from typing import TypedDict

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from openmarketer_core.agent_runtime.gate import RunScope
from openmarketer_core.db.chat import chat_history, create_chat
from openmarketer_core.db.chat_turns import (
    MAX_FOLLOW_UP_TURNS,
    ChatBusy,
    FollowUpGivenUp,
    start_turn,
)
from openmarketer_core.db.content_items import (
    NewContentItem,
    settle_content_item,
    store_proposed_content_item,
)
from openmarketer_core.db.media_assets import NewAssetVersion, add_version, record_version_review
from openmarketer_core.db.models import (
    AssetTechnique,
    ChatRole,
    ChatTurnTrigger,
    ContentItemStatus,
    ProductProfileRecord,
    Project,
    Workspace,
)
from openmarketer_core.db.research_runs import (
    ResearchRunNotFound,
    mark_run_failed,
    request_research_run,
    research_run,
)
from openmarketer_core.db.run_events import append_events, events_of_chat
from openmarketer_core.db.session import session_factory, transaction
from openmarketer_core.db.studio_runs import StudioRunNotFound, request_studio_run
from openmarketer_core.db.studio_runs import mark_run_failed as mark_studio_failed
from openmarketer_core.db.studio_runs import mark_run_needs_review as mark_studio_needs_review
from openmarketer_core.db.studio_runs import mark_run_started as mark_studio_started
from openmarketer_core.db.studio_runs import mark_run_succeeded as mark_studio_succeeded
from openmarketer_core.db.writing_runs import WritingRunNotFound, request_writing_run
from openmarketer_core.db.writing_runs import mark_run_failed as mark_writing_failed
from openmarketer_core.db.writing_runs import mark_run_started as mark_writing_started
from openmarketer_core.db.writing_runs import mark_run_succeeded as mark_writing_succeeded
from openmarketer_core.llm import ChatReply
from openmarketer_core.orchestrator import follow_up as follow_up_module
from openmarketer_core.orchestrator.chat_agent.rules import SUBMIT, ChatTurnError
from openmarketer_core.orchestrator.follow_up import (
    NOT_TAKEN,
    RunNotEnded,
    follow_up_on_run,
    record_follow_up_not_taken,
)
from openmarketer_core.orchestrator.reads_postgres import PostgresChatReads
from openmarketer_core.research.questions import ResearchRequest
from openmarketer_core.research.request import REQUESTED_BY_THE_CHAT
from openmarketer_core.research.workflow_contract import RESEARCH_RUN_KIND
from openmarketer_core.run_events import ESCALATED, TURN_ANSWERED, TURN_STARTED, RunEvent
from openmarketer_core.studio.asset_store import asset_key
from openmarketer_core.studio.platforms import Platform
from openmarketer_core.studio.requests import AssetRequest
from openmarketer_core.studio.workflow_contract import STUDIO_RUN_KIND
from openmarketer_core.writing.brief import WritingBrief
from openmarketer_core.writing.workflow_contract import WRITING_RUN_KIND

ASSET_REQUEST = AssetRequest(
    goal="Announce the Android launch", exact_copy=["Now on Android"], platform=Platform.X_POST
)


BRIEF = WritingBrief.model_validate({"platform": "x", "goal": "introduce"})


def call(name: str, **arguments) -> dict:
    return {"id": f"c{id(arguments)}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}  # fmt: skip


class ScriptedModel:
    """Replays prepared assistant turns and records what it was sent."""

    def __init__(self, *turns):
        self.turns = list(turns)
        self.requests: list[dict] = []

    def chat(self, role, messages, *, tools=None, tool_choice=None):
        self.requests.append({"messages": [dict(m) for m in messages], "tools": tools})
        turn = self.turns.pop(0) if self.turns else [call("get_project_status")]
        if isinstance(turn, Exception):
            raise turn
        message = {"role": "assistant", "content": None, "tool_calls": turn}
        return ChatReply(message=message, model="fake/model", cost_usd=0.01)


@pytest.fixture
def sessions(engine) -> sessionmaker[Session]:
    return session_factory(engine.url.render_as_string(hide_password=False))


@pytest.fixture
def scope(sessions) -> RunScope:
    with transaction(sessions) as session:
        workspace = Workspace(name="Acme")
        session.add(workspace)
        session.flush()
        project = Project(
            workspace_id=workspace.id, name="Example", source_repo_url="https://example.com/r.git"
        )
        session.add(project)
        session.flush()
        return RunScope(workspace_id=workspace.id, project_id=project.id)


@pytest.fixture
def chat_id(sessions, scope) -> uuid.UUID:
    with transaction(sessions) as session:
        return create_chat(session, workspace_id=scope.workspace_id, project_id=scope.project_id).id


@pytest.fixture
def run_id(sessions, scope, chat_id) -> uuid.UUID:
    """A research run the chat asked for, which failed."""
    with transaction(sessions) as session:
        run = request_research_run(
            session,
            workspace_id=scope.workspace_id,
            project_id=scope.project_id,
            request=ResearchRequest(questions=["Who are the competitors?"]),
            requested_by=REQUESTED_BY_THE_CHAT,
            chat_thread_id=chat_id,
        )
        mark_run_failed(
            session,
            workspace_id=scope.workspace_id,
            project_id=scope.project_id,
            run_id=run.id,
            error="the research could not be handed to a worker",
        )
        return run.id


def follow_up(
    sessions,
    scope: RunScope,
    chat_id: uuid.UUID,
    run_id: uuid.UUID,
    model,
    *,
    run_kind: str = RESEARCH_RUN_KIND,
):
    with transaction(sessions) as reading:
        return follow_up_on_run(
            sessions,
            PostgresChatReads(reading, scope.workspace_id, scope.project_id),
            workspace_id=scope.workspace_id,
            project_id=scope.project_id,
            chat_id=chat_id,
            run_kind=run_kind,
            run_id=run_id,
            model=model,
        )


def history(sessions, scope: RunScope, chat_id: uuid.UUID):
    with transaction(sessions) as session:
        return chat_history(
            session, workspace_id=scope.workspace_id, project_id=scope.project_id, chat_id=chat_id
        )


def test_a_follow_up_stores_one_answer_with_no_question_before_it(sessions, scope, chat_id, run_id):
    done = follow_up(
        sessions, scope, chat_id, run_id, ScriptedModel([call(SUBMIT, answer="It failed.")])
    )

    assert done is not None
    [message] = history(sessions, scope, chat_id)
    assert (message.role, message.content) == (ChatRole.ASSISTANT, "It failed.")
    assert message.id == done.answer.id
    assert message.turn_id is not None


def test_a_follow_up_records_its_turn_as_one_that_followed_up(sessions, scope, chat_id, run_id):
    done = follow_up(sessions, scope, chat_id, run_id, ScriptedModel([call(SUBMIT, answer="x")]))
    assert done is not None

    with transaction(sessions) as session:
        recorded = events_of_chat(
            session,
            workspace_id=scope.workspace_id,
            project_id=scope.project_id,
            chat_id=chat_id,
            after=None,
            limit=100,
        )
    assert [(event.kind, event.detail) for event in recorded] == [
        (TURN_STARTED, {"trigger": "research_finished"}),
        (TURN_ANSWERED, {"message_id": str(done.answer.id)}),
    ]


def test_the_model_is_told_of_the_run_and_the_instruction_is_not_stored(
    sessions, scope, chat_id, run_id
):
    model = ScriptedModel([call(SUBMIT, answer="It failed.")])

    follow_up(sessions, scope, chat_id, run_id, model)

    instruction = model.requests[0]["messages"][-1]
    assert instruction["role"] == "user"
    assert str(run_id) in instruction["content"]
    assert "failed" in instruction["content"]
    assert "read_research_report" in instruction["content"]
    assert [message.content for message in history(sessions, scope, chat_id)] == ["It failed."]


def test_a_follow_up_is_not_offered_to_start_another_run(sessions, scope, chat_id, run_id):
    model = ScriptedModel([call(SUBMIT, answer="It failed.")])

    follow_up(sessions, scope, chat_id, run_id, model)

    offered = [schema["function"]["name"] for schema in model.requests[0]["tools"]]
    assert "start_research" not in offered
    assert "read_research_report" in offered


def test_a_follow_up_that_calls_start_research_anyway_starts_nothing(
    sessions, scope, chat_id, run_id
):
    model = ScriptedModel(
        [call("start_research", questions=["Again?"])], [call(SUBMIT, answer="Not started.")]
    )

    follow_up(sessions, scope, chat_id, run_id, model)

    [result] = [m for m in model.requests[1]["messages"] if m["role"] == "tool"]
    assert result["content"].startswith("error:")


def test_a_follow_up_asked_for_again_after_it_answered_does_nothing(
    sessions, scope, chat_id, run_id
):
    follow_up(sessions, scope, chat_id, run_id, ScriptedModel([call(SUBMIT, answer="Once.")]))
    again = ScriptedModel([call(SUBMIT, answer="Twice.")])

    assert follow_up(sessions, scope, chat_id, run_id, again) is None
    assert again.requests == []
    assert [message.content for message in history(sessions, scope, chat_id)] == ["Once."]


def test_a_follow_up_that_failed_is_taken_again_when_asked_again(sessions, scope, chat_id, run_id):
    with pytest.raises(ChatTurnError):
        follow_up(sessions, scope, chat_id, run_id, ScriptedModel(ChatTurnError("no answer")))

    done = follow_up(sessions, scope, chat_id, run_id, ScriptedModel([call(SUBMIT, answer="x")]))

    assert done is not None


def test_a_follow_up_in_a_busy_chat_is_busy(sessions, scope, chat_id, run_id):
    with transaction(sessions) as session:
        start_turn(
            session,
            workspace_id=scope.workspace_id,
            project_id=scope.project_id,
            chat_id=chat_id,
            trigger=ChatTurnTrigger.MESSAGE,
        )
    model = ScriptedModel([call(SUBMIT, answer="unreached")])

    with pytest.raises(ChatBusy):
        follow_up(sessions, scope, chat_id, run_id, model)

    assert model.requests == []


def test_a_follow_up_on_a_run_the_chat_did_not_ask_for_is_not_found(sessions, scope, chat_id):
    model = ScriptedModel([call(SUBMIT, answer="unreached")])

    with pytest.raises(ResearchRunNotFound):
        follow_up(sessions, scope, chat_id, uuid.uuid4(), model)

    assert model.requests == []


def test_a_run_that_has_not_ended_has_nothing_to_follow_up_on(sessions, scope, chat_id):
    with transaction(sessions) as session:
        queued = request_research_run(
            session,
            workspace_id=scope.workspace_id,
            project_id=scope.project_id,
            request=ResearchRequest(questions=["Who are the competitors?"]),
            requested_by=REQUESTED_BY_THE_CHAT,
            chat_thread_id=chat_id,
        ).id
    model = ScriptedModel([call(SUBMIT, answer="unreached")])

    with pytest.raises(RunNotEnded):
        follow_up(sessions, scope, chat_id, queued, model)

    assert model.requests == []


def test_an_attempt_that_answers_while_another_prepares_stops_the_other_at_its_start(
    sessions, scope, chat_id, run_id, monkeypatch
):
    # Attempt A answers between attempt B's first read and B's turn: B must not answer again.
    read_the_run = research_run
    first = ScriptedModel([call(SUBMIT, answer="From A.")])

    def read_while_a_answers(*args, **kwargs):
        monkeypatch.setattr(follow_up_module, "research_run", read_the_run)
        follow_up(sessions, scope, chat_id, run_id, first)
        return read_the_run(*args, **kwargs)

    monkeypatch.setattr(follow_up_module, "research_run", read_while_a_answers)
    second = ScriptedModel([call(SUBMIT, answer="From B.")])

    assert follow_up(sessions, scope, chat_id, run_id, second) is None
    assert second.requests == []
    assert [message.content for message in history(sessions, scope, chat_id)] == ["From A."]


def test_a_follow_up_that_failed_its_last_allowed_turn_is_given_up(
    sessions, scope, chat_id, run_id
):
    for _ in range(MAX_FOLLOW_UP_TURNS):
        with pytest.raises(ChatTurnError):
            follow_up(sessions, scope, chat_id, run_id, ScriptedModel(ChatTurnError("no")))
    model = ScriptedModel([call(SUBMIT, answer="unreached")])

    with pytest.raises(FollowUpGivenUp):
        follow_up(sessions, scope, chat_id, run_id, model)

    assert model.requests == []


def test_a_follow_up_not_taken_tells_the_chat_why_in_a_fixed_sentence(
    sessions, scope, chat_id, run_id
):
    recorded = record_follow_up_not_taken(
        sessions,
        workspace_id=scope.workspace_id,
        project_id=scope.project_id,
        chat_id=chat_id,
        run_kind=RESEARCH_RUN_KIND,
        run_id=run_id,
        cause="the chat stayed busy",
    )

    assert recorded
    with transaction(sessions) as session:
        last = events_of_chat(
            session,
            workspace_id=scope.workspace_id,
            project_id=scope.project_id,
            chat_id=chat_id,
            after=None,
            limit=100,
        )[-1]
    assert last.detail == {
        "reason": NOT_TAKEN[RESEARCH_RUN_KIND].format(cause="the chat stayed busy")
    }
    assert list(history(sessions, scope, chat_id)) == []


# -------------------------------------------------------------------- studio runs
class ScopeIds(TypedDict):
    """The two ids the scoped functions take, so unpacking them names ids and nothing else."""

    workspace_id: uuid.UUID
    project_id: uuid.UUID


def studio_run_of_chat(sessions, scope: RunScope, chat_id: uuid.UUID, ending: str) -> uuid.UUID:
    """A studio run the chat asked for, ended as ``ending`` says, with one reviewed image."""
    ids = ScopeIds(workspace_id=scope.workspace_id, project_id=scope.project_id)
    with transaction(sessions) as session:
        run = request_studio_run(
            session,
            **ids,
            request=ASSET_REQUEST,
            requested_by=REQUESTED_BY_THE_CHAT,
            chat_thread_id=chat_id,
        )
        if ending == "failed":
            mark_studio_failed(session, **ids, run_id=run.id, error="the image model refused")
            return run.id
        mark_studio_started(session, **ids, run_id=run.id)
        version_id = uuid.uuid4()
        add_version(
            session,
            **ids,
            run_id=run.id,
            version_id=version_id,
            action_id=f"studio-run:{run.id}:1",
            version=NewAssetVersion(
                round=1,
                technique=AssetTechnique.GENERATED,
                storage_key=asset_key(
                    **ids, run_id=run.id, version_id=version_id, mime="image/png"
                ),
                mime="image/png",
                width=1600,
                height=900,
                size_bytes=1024,
                cost_usd=Decimal("0.04"),
            ),
        )
        passed = ending == "succeeded"
        record_version_review(
            session,
            **ids,
            version_id=version_id,
            check_report={"passed": passed, "failures": [] if passed else ["copy"]},
            verifier_report={
                "criteria": [
                    {"criterion": "legibility", "passed": passed, "severity": "major", "note": ""}
                ],
                "edit_suggestions": [],
            },
            passed=passed,
        )
        if passed:
            mark_studio_succeeded(
                session, **ids, run_id=run.id, final_version_id=version_id, cost_usd=Decimal("0.04")
            )
            return run.id
        mark_studio_needs_review(
            session, **ids, run_id=run.id, final_version_id=version_id, cost_usd=Decimal("0.04")
        )
        append_events(
            session,
            **ids,
            run_kind=STUDIO_RUN_KIND,
            run_id=run.id,
            events=[RunEvent(ESCALATED, {"version_id": str(version_id), "reason": "round limit"})],
        )
        return run.id


def follow_up_on_studio(sessions, scope, chat_id, run_id, *turns):
    model = ScriptedModel(
        [call("get_studio_run", run_id=str(run_id))], *turns or [[call(SUBMIT, answer="Done.")]]
    )
    done = follow_up(sessions, scope, chat_id, run_id, model, run_kind=STUDIO_RUN_KIND)
    return done, model


@pytest.mark.parametrize("ending", ["succeeded", "needs_review", "failed"])
def test_a_studio_follow_up_is_told_how_the_run_ended(sessions, scope, chat_id, ending):
    run_id = studio_run_of_chat(sessions, scope, chat_id, ending)

    done, model = follow_up_on_studio(sessions, scope, chat_id, run_id)

    assert done is not None
    instruction = model.requests[0]["messages"][-1]["content"]
    assert f"studio run {run_id}" in instruction
    assert f"status {ending}" in instruction
    assert "get_studio_run" in instruction
    assert [message.content for message in history(sessions, scope, chat_id)] == ["Done."]


def test_a_studio_follow_up_reads_the_image_waiting_in_the_library(sessions, scope, chat_id):
    run_id = studio_run_of_chat(sessions, scope, chat_id, "succeeded")

    _, model = follow_up_on_studio(sessions, scope, chat_id, run_id)

    [result] = [m for m in model.requests[1]["messages"] if m["role"] == "tool"]
    run = json.loads(result["content"])
    assert run["status"] == "succeeded"
    assert run["final_asset_id"] is not None
    assert run["review"]["passed"] is True
    assert run["review_reason"] is None


def test_a_studio_follow_up_reads_why_the_run_needs_review(sessions, scope, chat_id):
    run_id = studio_run_of_chat(sessions, scope, chat_id, "needs_review")

    _, model = follow_up_on_studio(sessions, scope, chat_id, run_id)

    [result] = [m for m in model.requests[1]["messages"] if m["role"] == "tool"]
    run = json.loads(result["content"])
    assert run["review_reason"] == "round limit"
    assert run["review"]["check_failures"] == ["copy"]
    assert run["review"]["failed_criteria"] == [
        {"criterion": "legibility", "severity": "major", "note": ""}
    ]


def test_a_studio_follow_up_reads_why_the_run_failed(sessions, scope, chat_id):
    run_id = studio_run_of_chat(sessions, scope, chat_id, "failed")

    _, model = follow_up_on_studio(sessions, scope, chat_id, run_id)

    [result] = [m for m in model.requests[1]["messages"] if m["role"] == "tool"]
    run = json.loads(result["content"])
    assert (run["status"], run["error"], run["final_asset_id"]) == (
        "failed",
        "the image model refused",
        None,
    )


def test_a_studio_follow_up_is_a_studio_finished_turn(sessions, scope, chat_id):
    run_id = studio_run_of_chat(sessions, scope, chat_id, "succeeded")

    follow_up_on_studio(sessions, scope, chat_id, run_id)

    with transaction(sessions) as session:
        recorded = events_of_chat(
            session,
            workspace_id=scope.workspace_id,
            project_id=scope.project_id,
            chat_id=chat_id,
            after=None,
            limit=100,
        )
    started = [event.detail for event in recorded if event.kind == TURN_STARTED]
    assert started == [{"trigger": "studio_finished"}]


def test_a_studio_follow_up_is_taken_once(sessions, scope, chat_id):
    run_id = studio_run_of_chat(sessions, scope, chat_id, "succeeded")
    follow_up_on_studio(sessions, scope, chat_id, run_id)
    again = ScriptedModel([call(SUBMIT, answer="Twice.")])

    assert follow_up(sessions, scope, chat_id, run_id, again, run_kind=STUDIO_RUN_KIND) is None
    assert again.requests == []


def test_a_studio_run_that_has_not_ended_has_nothing_to_follow_up_on(sessions, scope, chat_id):
    with transaction(sessions) as session:
        queued = request_studio_run(
            session,
            workspace_id=scope.workspace_id,
            project_id=scope.project_id,
            request=ASSET_REQUEST,
            requested_by=REQUESTED_BY_THE_CHAT,
            chat_thread_id=chat_id,
        ).id
    model = ScriptedModel([call(SUBMIT, answer="unreached")])

    with pytest.raises(RunNotEnded):
        follow_up(sessions, scope, chat_id, queued, model, run_kind=STUDIO_RUN_KIND)

    assert model.requests == []


def test_a_studio_follow_up_on_a_run_the_chat_did_not_ask_for_is_not_found(
    sessions, scope, chat_id
):
    model = ScriptedModel([call(SUBMIT, answer="unreached")])

    with pytest.raises(StudioRunNotFound):
        follow_up(sessions, scope, chat_id, uuid.uuid4(), model, run_kind=STUDIO_RUN_KIND)


def test_a_follow_up_on_another_kind_of_run_is_refused_before_anything_is_read(
    sessions, scope, chat_id, run_id
):
    model = ScriptedModel([call(SUBMIT, answer="unreached")])

    with pytest.raises(ValueError):
        follow_up(sessions, scope, chat_id, run_id, model, run_kind="analysis-run")

    assert model.requests == []


def test_a_studio_follow_up_not_taken_tells_the_chat_why(sessions, scope, chat_id):
    run_id = studio_run_of_chat(sessions, scope, chat_id, "failed")

    recorded = record_follow_up_not_taken(
        sessions,
        workspace_id=scope.workspace_id,
        project_id=scope.project_id,
        chat_id=chat_id,
        run_kind=STUDIO_RUN_KIND,
        run_id=run_id,
        cause="no worker took it",
    )

    assert recorded
    with transaction(sessions) as session:
        recorded_events = events_of_chat(
            session,
            workspace_id=scope.workspace_id,
            project_id=scope.project_id,
            chat_id=chat_id,
            after=None,
            limit=100,
        )
    assert recorded_events[0].detail == {"trigger": "studio_finished"}
    assert recorded_events[-1].detail == {
        "reason": NOT_TAKEN[STUDIO_RUN_KIND].format(cause="no worker took it")
    }


# -------------------------------------------------------------------- writing runs
def writing_run_of_chat(sessions, scope: RunScope, chat_id: uuid.UUID, ending: str) -> uuid.UUID:
    """A writing run the chat asked for: failed before it began, or its post settled as
    ``ending`` (``dry_run``, ``rejected``, ``expired``) and the run succeeded."""
    ids = ScopeIds(workspace_id=scope.workspace_id, project_id=scope.project_id)
    with transaction(sessions) as session:
        run = request_writing_run(
            session,
            **ids,
            brief=BRIEF,
            requested_by=REQUESTED_BY_THE_CHAT,
            chat_thread_id=chat_id,
        )
        if ending == "failed":
            mark_writing_failed(session, **ids, run_id=run.id, error="the profile is not approved")
            return run.id
        mark_writing_started(
            session, **ids, run_id=run.id, profile_version_id=approved_version(session, scope)
        )
        item = store_proposed_content_item(
            session,
            **ids,
            run_id=run.id,
            item=NewContentItem(
                text="Memoria keeps your notes, offline too.",
                plan={"angle": "offline"},
                check_report={"passed": True, "failures": []},
                critic_report={"variant": 0, "risky_claim": False, "criteria": []},
                cost_usd=Decimal("0.02"),
            ),
        )
        settle_content_item(session, **ids, item_id=item.id, status=ContentItemStatus(ending))
        mark_writing_succeeded(session, **ids, run_id=run.id, cost_usd=Decimal("0.02"))
        return run.id


def approved_version(session: Session, scope: RunScope) -> uuid.UUID:
    """An approved version of the project's profile: inserted as a draft, approved by UPDATE."""
    record = ProductProfileRecord(
        project_id=scope.project_id, version=1, content={"product": {"name": "Memoria"}}
    )
    session.add(record)
    session.flush()
    session.execute(
        text(
            "UPDATE product_profile SET status = 'approved', approved_by = :by,"
            " approved_at = now() WHERE id = :id"
        ),
        {"by": uuid.uuid4(), "id": record.id},
    )
    return record.id


def follow_up_on_writing(sessions, scope, chat_id, run_id):
    model = ScriptedModel(
        [call("get_writing_run", run_id=str(run_id))], [call(SUBMIT, answer="Done.")]
    )
    done = follow_up(sessions, scope, chat_id, run_id, model, run_kind=WRITING_RUN_KIND)
    return done, model


@pytest.mark.parametrize("ending", ["dry_run", "rejected", "expired", "failed"])
def test_a_writing_follow_up_is_told_how_the_run_ended(sessions, scope, chat_id, ending):
    run_id = writing_run_of_chat(sessions, scope, chat_id, ending)

    done, model = follow_up_on_writing(sessions, scope, chat_id, run_id)

    assert done is not None
    instruction = model.requests[0]["messages"][-1]["content"]
    status = "failed" if ending == "failed" else "succeeded"
    assert f"writing run {run_id}" in instruction
    assert f"status {status}" in instruction
    assert "get_writing_run" in instruction
    assert [message.content for message in history(sessions, scope, chat_id)] == ["Done."]


def test_a_writing_follow_up_reads_how_the_post_was_settled(sessions, scope, chat_id):
    run_id = writing_run_of_chat(sessions, scope, chat_id, "dry_run")

    _, model = follow_up_on_writing(sessions, scope, chat_id, run_id)

    [result] = [m for m in model.requests[1]["messages"] if m["role"] == "tool"]
    run = json.loads(result["content"])
    assert (run["status"], run["post"]["status"], run["post"]["proposal_id"]) == (
        "succeeded",
        "dry_run",
        None,
    )
    assert run["post"]["text"] == "Memoria keeps your notes, offline too."


def test_a_writing_follow_up_reads_why_the_run_failed(sessions, scope, chat_id):
    run_id = writing_run_of_chat(sessions, scope, chat_id, "failed")

    _, model = follow_up_on_writing(sessions, scope, chat_id, run_id)

    [result] = [m for m in model.requests[1]["messages"] if m["role"] == "tool"]
    run = json.loads(result["content"])
    assert (run["status"], run["error"], run["post"]) == (
        "failed",
        "the profile is not approved",
        None,
    )


def test_a_writing_follow_up_is_a_writing_finished_turn(sessions, scope, chat_id):
    run_id = writing_run_of_chat(sessions, scope, chat_id, "rejected")

    follow_up_on_writing(sessions, scope, chat_id, run_id)

    with transaction(sessions) as session:
        recorded = events_of_chat(
            session,
            workspace_id=scope.workspace_id,
            project_id=scope.project_id,
            chat_id=chat_id,
            after=None,
            limit=100,
        )
    started = [event.detail for event in recorded if event.kind == TURN_STARTED]
    assert started == [{"trigger": "writing_finished"}]


def test_a_writing_run_that_has_not_ended_has_nothing_to_follow_up_on(sessions, scope, chat_id):
    with transaction(sessions) as session:
        queued = request_writing_run(
            session,
            workspace_id=scope.workspace_id,
            project_id=scope.project_id,
            brief=BRIEF,
            requested_by=REQUESTED_BY_THE_CHAT,
            chat_thread_id=chat_id,
        ).id
    model = ScriptedModel([call(SUBMIT, answer="unreached")])

    with pytest.raises(RunNotEnded):
        follow_up(sessions, scope, chat_id, queued, model, run_kind=WRITING_RUN_KIND)

    assert model.requests == []


def test_a_writing_follow_up_on_a_run_the_chat_did_not_ask_for_is_not_found(
    sessions, scope, chat_id
):
    model = ScriptedModel([call(SUBMIT, answer="unreached")])

    with pytest.raises(WritingRunNotFound):
        follow_up(sessions, scope, chat_id, uuid.uuid4(), model, run_kind=WRITING_RUN_KIND)


def test_a_writing_follow_up_not_taken_tells_the_chat_why(sessions, scope, chat_id):
    run_id = writing_run_of_chat(sessions, scope, chat_id, "failed")

    recorded = record_follow_up_not_taken(
        sessions,
        workspace_id=scope.workspace_id,
        project_id=scope.project_id,
        chat_id=chat_id,
        run_kind=WRITING_RUN_KIND,
        run_id=run_id,
        cause="no worker took it",
    )

    assert recorded
    with transaction(sessions) as session:
        recorded_events = events_of_chat(
            session,
            workspace_id=scope.workspace_id,
            project_id=scope.project_id,
            chat_id=chat_id,
            after=None,
            limit=100,
        )
    assert recorded_events[0].detail == {"trigger": "writing_finished"}
    assert recorded_events[-1].detail == {
        "reason": NOT_TAKEN[WRITING_RUN_KIND].format(cause="no worker took it")
    }
