"""Tests for the orchestrator task's graph, run step by step with its actions answered here.

The role ``orchestrator`` is scripted (``ScriptedChat``), the approved profile is a fixture, and
each action is answered as the runtime would answer it: ``confirm_plan`` approved, rejected or
expired by a person, and each run action with the final state of a child run, or as a run that
did not finish. The thread is kept in memory by the strict serializer the PostgreSQL store uses,
so a state that is not plain data fails here as it would in the worker. Nothing reaches a
network or a database.
"""

import json
import subprocess
import sys
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from openmarketer_core.agent_runtime.actions import ActionEffect, ActionOutcome, RunKind
from openmarketer_core.agent_runtime.gate import (
    NOT_APPROVED_TEXTS,
    GateVerdict,
    ProposalStatus,
    verdict_for,
)
from openmarketer_core.agent_runtime.langgraph_agent import AgentSteps
from openmarketer_core.agent_runtime.steps import GraphRunRef
from openmarketer_core.agent_runtime.threads import thread_id
from openmarketer_core.graph_checkpoints import RunCheckpoints
from openmarketer_core.llm import ChatReply
from openmarketer_core.orchestrator.task.failures import TaskError
from openmarketer_core.orchestrator.task.plan import (
    ChatLine,
    ResearchReportAtHand,
    TaskAtHand,
    TaskPlanChange,
    TaskRequest,
)
from openmarketer_core.orchestrator.task.task_agent import progress
from openmarketer_core.orchestrator.task.task_agent.definitions import (
    CONFIRM_PLAN,
    RUN_RESEARCH,
    RUN_STUDIO,
    RUN_WRITING,
    TaskContext,
    agent,
    initial_state,
    result_of,
)
from openmarketer_core.orchestrator.task.task_agent.graph import recursion_limit
from openmarketer_core.orchestrator.task.task_agent.rules import TaskLimits
from openmarketer_core.orchestrator.task.workflow_contract import TASK_RUN_KIND, TaskRunInput
from openmarketer_core.profile import ProductProfile
from openmarketer_core.research.workflow_contract import RESEARCH_TASK_QUEUE, RESEARCH_WORKFLOW
from openmarketer_core.run_events import RunEvent
from openmarketer_core.studio.workflow_contract import STUDIO_TASK_QUEUE, STUDIO_WORKFLOW
from openmarketer_core.writing.workflow_contract import WRITING_TASK_QUEUE, WRITING_WORKFLOW

STARTED = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
STUDIO_RUN_ID = "11111111-1111-4111-8111-111111111111"
ASSET_ID = "22222222-2222-4222-8222-222222222222"
WRITING_RUN_ID = "33333333-3333-4333-8333-333333333333"
ITEM_ID = "44444444-4444-4444-8444-444444444444"
RESEARCH_RUN_ID = "55555555-5555-4555-8555-555555555555"
REPORT_RUN_ID = "99999999-9999-4999-8999-999999999999"
SECOND_WRITING_RUN_ID = "66666666-6666-4666-8666-666666666666"
SECOND_ITEM_ID = "77777777-7777-4777-8777-777777777777"

POST_WITH_IMAGE = 'Write a post announcing offline sync, with an image that says "Write anywhere".'


def a_profile() -> ProductProfile:
    return ProductProfile.model_validate(
        {
            "product": {"name": "Acme Notes", "type": "b2b_saas", "languages": ["en"]},
            "features": [
                {
                    "id": "offline-sync",
                    "description": "Notes sync when the device is back online.",
                    "status": "live",
                    "evidence": [{"file": "src/sync.ts", "lines": "10-40"}],
                    "confidence": 0.9,
                },
                {"id": "ai-summary", "description": "Summaries.", "status": "unreleased"},
            ],
            "brand": {"voice": "Plain and warm.", "do_not_mention": ["BetaCorp"]},
        }
    )


# ------------------------------------------------------------------------ the scripted model
class ScriptedChat:
    """Prepared answers of the role ``orchestrator``, in order; each call is recorded."""

    def __init__(self, *answers: ChatReply):
        self.answers = list(answers)
        self.calls: list[dict[str, Any]] = []

    def chat(self, role, messages, *, tools=None, tool_choice=None) -> ChatReply:
        self.calls.append({"role": role, "messages": messages, "tools": tools})
        return self.answers.pop(0)


def text(content: Any, *, cost_usd: float = 0.01, finish_reason: str = "stop") -> ChatReply:
    body = content if isinstance(content, str) else json.dumps(content)
    return ChatReply(
        message={"role": "assistant", "content": body},
        model="vendor/m",
        cost_usd=cost_usd,
        finish_reason=finish_reason,
    )


def scope(**changes: Any) -> dict[str, Any]:
    return {"max_steps": 4, "max_plan_changes": 2, "deadline_days": 7, "max_cost_usd": 1, **changes}


def studio_step(step_id: str = "image", **request: Any) -> dict[str, Any]:
    return {
        "id": step_id,
        "agent": "studio",
        "request": {
            "goal": "Notes syncing on a plane.",
            "exact_copy": ["Write anywhere"],
            "platform": "x_post",
            "style_notes": "",
            **request,
        },
    }


def writing_step(step_id: str = "post", **fields: Any) -> dict[str, Any]:
    return {
        "id": step_id,
        "agent": "writing",
        "request": {"platform": "x", "goal": "announce", "feature": "offline-sync", "variants": 2},
        **fields,
    }


def research_step(step_id: str = "research") -> dict[str, Any]:
    return {
        "id": step_id,
        "agent": "research",
        "request": {"questions": ["What do remote teams struggle with?"]},
    }


def a_plan(*steps: dict[str, Any], **scope_changes: Any) -> dict[str, Any]:
    return {"goal": "Announce offline sync.", "steps": list(steps), "scope": scope(**scope_changes)}


def a_review(decision: str = "finish", plan: dict[str, Any] | None = None) -> ChatReply:
    return text({"decision": decision, "reason": "Done.", "plan": plan})


def data_of(messages: list[dict[str, Any]]) -> dict[str, Any]:
    return json.loads(messages[1]["content"].split("Data:\n", 1)[1])


# ---------------------------------------------------------------------- the children's ends
def studio_end(**changes: Any) -> dict[str, Any]:
    return {
        "run_id": STUDIO_RUN_ID,
        "asset_id": ASSET_ID,
        "result": {"final_version_id": str(uuid.uuid4()), "needs_review": False, "reason": ""},
        "cost_usd": 0.2,
        **changes,
    }


def writing_end(
    status: str = "dry_run", run_id: str = WRITING_RUN_ID, item: str = ITEM_ID
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "result": {"status": status, "content_item_id": item, "text": "Write anywhere."},
        "cost_usd": 0.05,
    }


def research_end() -> dict[str, Any]:
    return {
        "run_id": RESEARCH_RUN_ID,
        "findings": [{"statement": "Remote teams lose notes offline.", "confidence": 0.8}],
        "cost_usd": 0.1,
    }


DID_NOT_FINISH = "the run it started did not finish"


# ------------------------------------------------------------------------------- the run
class Run:
    """One task to its end, its actions answered from ``children`` and ``confirmations``."""

    def __init__(
        self,
        chat: ScriptedChat,
        *,
        request: str = POST_WITH_IMAGE,
        children: Mapping[str, list[dict[str, Any] | str]] | None = None,
        confirmations: list[str] | None = None,
        profile: ProductProfile | None = None,
        limits: TaskLimits | None = None,
        now: datetime = STARTED + timedelta(hours=1),
        cost_usd: float = 0.0,
        reports: tuple[ResearchReportAtHand, ...] = (),
        chat_history: tuple[ChatLine, ...] = (),
    ):
        run = TaskRunInput(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
        self.run = run
        self.children = {name: list(ends) for name, ends in (children or {}).items()}
        self.confirmations = list(confirmations or [])
        context = TaskContext(
            model=chat,
            at_hand=lambda: TaskAtHand(
                profile=profile or a_profile(), reports=reports, chat=chat_history
            ),
            now=lambda: now,
        )
        self.events: list[RunEvent] = []
        self.asked: list[tuple[str, dict[str, Any]]] = []
        finals: list[dict[str, Any]] = []

        def start(ref: GraphRunRef) -> dict[str, Any]:
            state = initial_state(
                run,
                TaskRequest(request=request, understanding="One post, with an image."),
                limits=limits or TaskLimits(),
                started_at=STARTED,
            )
            state["cost_usd"] = cost_usd
            return dict(state)

        def finish(ref: GraphRunRef, final: Mapping[str, Any]) -> dict[str, Any]:
            finals.append(dict(final))
            return {}

        steps = AgentSteps(
            agent,
            initial_state=start,
            finish=finish,
            context=lambda ref, checkpoints: context,
            recursion_limit=recursion_limit(),
            record_progress=lambda ref, events: self.events.extend(events),
        )
        ref = GraphRunRef(TASK_RUN_KIND, run.run_id)
        store = InMemorySaver(serde=JsonPlusSerializer(allowed_msgpack_modules=None))
        checkpoints = RunCheckpoints(store=store, thread_id=thread_id(ref.kind, ref.run_id))
        resume = None
        while True:
            step = steps.advance(ref, checkpoints, resume)
            if step.action is None:
                break
            self.asked.append((step.action.name, step.action.payload))
            resume = self._answer(step.action.name, step.action.action_id)
        [self.final] = finals
        self.result = result_of(self.final)

    def _answer(self, name: str, action_id: str) -> ActionOutcome:
        if name == CONFIRM_PLAN:
            decision = self.confirmations.pop(0) if self.confirmations else "approve"
            if decision == "approve":
                return ActionOutcome(action_id, True, {}, "")
            return ActionOutcome(action_id, False, {}, NOT_APPROVED_TEXTS[ProposalStatus(decision)])
        end = self.children[name].pop(0)
        if isinstance(end, str):
            return ActionOutcome(action_id, False, {}, end)
        return ActionOutcome(action_id, True, end, "")

    def names(self) -> list[str]:
        return [name for name, _ in self.asked]

    def payloads(self, name: str) -> list[dict[str, Any]]:
        return [payload for asked, payload in self.asked if asked == name]

    def kinds(self) -> list[str]:
        return [event.kind for event in self.events]


# --------------------------------------------------------------------------- declarations
def test_the_task_has_four_actions_and_no_other():
    assert {definition.name for definition in agent.definitions} == {
        CONFIRM_PLAN,
        RUN_RESEARCH,
        RUN_STUDIO,
        RUN_WRITING,
    }


def test_confirming_a_plan_is_held_by_the_gate_for_a_person():
    policy = agent.policies[CONFIRM_PLAN]
    assert policy.effect is ActionEffect.CONFIRMS
    assert verdict_for(policy) is GateVerdict.ASK_A_PERSON


def test_each_run_action_runs_its_agents_workflow_and_waits_for_it():
    targets = agent.run_targets
    assert {name: (t.workflow, t.task_queue, t.kind) for name, t in targets.items()} == {
        RUN_RESEARCH: (RESEARCH_WORKFLOW, RESEARCH_TASK_QUEUE, RunKind.WAIT),
        RUN_STUDIO: (STUDIO_WORKFLOW, STUDIO_TASK_QUEUE, RunKind.WAIT),
        RUN_WRITING: (WRITING_WORKFLOW, WRITING_TASK_QUEUE, RunKind.WAIT),
    }
    assert all(agent.policies[name].effect is ActionEffect.INTERNAL for name in targets)


def test_the_declarations_load_no_graph_framework_or_database():
    code = (
        "import sys, json; import openmarketer_core.orchestrator.task.task_agent.definitions; "
        "print(json.dumps(sorted(sys.modules)))"
    )
    done = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True
    )
    loaded = set(json.loads(done.stdout))
    for package in ("langgraph", "sqlalchemy", "psycopg"):
        assert not any(name == package or name.startswith(f"{package}.") for name in loaded)


# ------------------------------------------------------------------------------- the runs
def test_a_post_with_an_image_runs_the_studio_then_the_writer_with_its_asset():
    chat = ScriptedChat(
        text(a_plan(studio_step(), writing_step(asset_from_step="image", depends_on=["image"]))),
        a_review("dispatch_next"),
        a_review("finish"),
    )

    run = Run(chat, children={RUN_STUDIO: [studio_end()], RUN_WRITING: [writing_end()]})

    assert run.names() == [CONFIRM_PLAN, RUN_STUDIO, RUN_WRITING]
    [studio] = run.payloads(RUN_STUDIO)
    assert studio["request"]["exact_copy"] == ["Write anywhere"]
    assert studio["request"]["reference_asset_ids"] == []
    [writing] = run.payloads(RUN_WRITING)
    assert writing["brief"]["asset"] == ASSET_ID  # resolved by code from the studio's outcome
    assert writing["brief"]["research_run_id"] is None
    assert writing["task_id"] == str(run.run.run_id) and writing["step"] == "post"
    assert run.result.outcome == "completed"
    assert run.result.report["content_item_ids"] == [ITEM_ID]
    assert run.result.report["asset_ids"] == [ASSET_ID]
    assert run.result.total_cost_usd == pytest.approx(0.03 + 0.2 + 0.05)


def test_a_post_without_an_image_runs_the_writer_only_and_asks_nobody_to_confirm():
    chat = ScriptedChat(text(a_plan(writing_step())), a_review("finish"))

    run = Run(
        chat,
        request="Write a post announcing offline sync.",
        children={RUN_WRITING: [writing_end()]},
    )

    assert run.names() == [RUN_WRITING]
    [writing] = run.payloads(RUN_WRITING)
    assert writing["brief"]["asset"] is None
    assert run.result.outcome == "completed"


def test_research_reaches_the_writer_by_its_run_id():
    chat = ScriptedChat(
        text(a_plan(research_step(), writing_step(research_from_step="research"))),
        a_review("dispatch_next"),
        a_review("finish"),
    )

    run = Run(chat, children={RUN_RESEARCH: [research_end()], RUN_WRITING: [writing_end()]})

    [writing] = run.payloads(RUN_WRITING)
    assert writing["brief"]["research_run_id"] == RESEARCH_RUN_ID
    assert run.result.report["research_run_ids"] == [RESEARCH_RUN_ID]
    review = data_of(chat.calls[1]["messages"])
    assert review["steps"][0]["outcome"]["statements"] == ["Remote teams lose notes offline."]


def a_report_at_hand() -> ResearchReportAtHand:
    return ResearchReportAtHand(
        run_id=uuid.UUID(REPORT_RUN_ID),
        questions=("What do remote teams struggle with?",),
        statements=("Remote teams lose notes offline.",),
        findings=3,
        finished_at=STARTED,
    )


def test_a_finished_report_reaches_the_writer_without_a_research_step():
    chat = ScriptedChat(
        text(a_plan(writing_step(research_from_report="report-1"))), a_review("finish")
    )

    run = Run(
        chat,
        request="Write a post from the research we already have.",
        reports=(a_report_at_hand(),),
        chat_history=(ChatLine(role="user", content="Use the report about remote teams."),),
        children={RUN_WRITING: [writing_end()]},
    )

    [writing] = run.payloads(RUN_WRITING)
    assert writing["brief"]["research_run_id"] == REPORT_RUN_ID
    assert run.names() == [RUN_WRITING]
    planning = data_of(chat.calls[0]["messages"])
    assert planning["research_reports"][0]["label"] == "report-1"
    assert planning["research_reports"][0]["statements"] == ["Remote teams lose notes offline."]
    assert planning["research_reports"][0]["findings"] == 3
    assert planning["chat"] == [{"role": "user", "content": "Use the report about remote teams."}]
    assert REPORT_RUN_ID not in chat.calls[0]["messages"][1]["content"]


def test_a_plan_that_rests_on_a_report_the_project_lacks_is_refused_and_asked_once_more():
    chat = ScriptedChat(
        text(a_plan(writing_step(research_from_report="report-2"))),
        text(a_plan(writing_step())),
        a_review("finish"),
    )

    run = Run(
        chat,
        request="Write a post from the research we already have.",
        reports=(a_report_at_hand(),),
        children={RUN_WRITING: [writing_end()]},
    )

    told = chat.calls[1]["messages"][-1]["content"]
    assert told.startswith("Rejected.")
    assert "report-2" in told and "report-1" in told
    assert run.names() == [RUN_WRITING]


def test_a_plan_that_refers_to_a_later_step_is_refused_and_asked_once_more():
    wrong = a_plan(writing_step(asset_from_step="image"), studio_step())
    chat = ScriptedChat(
        text(wrong),
        text(a_plan(studio_step(), writing_step(asset_from_step="image"))),
        a_review("dispatch_next"),
        a_review("finish"),
    )

    run = Run(chat, children={RUN_STUDIO: [studio_end()], RUN_WRITING: [writing_end()]})

    told = chat.calls[1]["messages"][-1]["content"]
    assert told.startswith("Rejected.")
    assert "asset_from_step of step 'post'" in told and "earlier step" in told
    assert run.names() == [CONFIRM_PLAN, RUN_STUDIO, RUN_WRITING]


def test_a_plan_refused_twice_ends_the_task():
    wrong = text(a_plan(writing_step(asset_from_step="image"), studio_step()))
    chat = ScriptedChat(wrong, wrong)

    with pytest.raises(TaskError, match="could not be read after two attempts"):
        Run(chat)


def test_a_reference_to_a_step_of_another_agent_is_refused():
    chat = ScriptedChat(
        text(a_plan(research_step(), writing_step(asset_from_step="research"))),
        text(a_plan(research_step(), writing_step(research_from_step="research"))),
        a_review("dispatch_next"),
        a_review("finish"),
    )

    Run(chat, children={RUN_RESEARCH: [research_end()], RUN_WRITING: [writing_end()]})

    assert "which is not a studio step" in chat.calls[1]["messages"][-1]["content"]


def test_a_studio_step_holds_the_plan_for_a_persons_confirmation_with_the_plan_in_it():
    chat = ScriptedChat(text(a_plan(studio_step())), a_review("finish"))

    run = Run(chat, children={RUN_STUDIO: [studio_end()]})

    [confirmation] = run.payloads(CONFIRM_PLAN)
    assert confirmation["confirmation"] == 1
    assert confirmation["task_id"] == str(run.run.run_id)
    assert confirmation["plan"]["steps"][0]["agent"] == "studio"
    assert run.final["confirmation"] == "confirmed"


def test_a_scope_above_the_defaults_is_held_for_confirmation():
    chat = ScriptedChat(text(a_plan(writing_step(), deadline_days=14)), a_review("finish"))

    run = Run(chat, children={RUN_WRITING: [writing_end()]})

    assert run.names() == [CONFIRM_PLAN, RUN_WRITING]


def test_a_rejected_confirmation_ends_the_task_with_nothing_run():
    chat = ScriptedChat(text(a_plan(studio_step(), writing_step(asset_from_step="image"))))

    run = Run(chat, confirmations=["rejected"])

    assert run.names() == [CONFIRM_PLAN]
    assert run.result.outcome == "plan_rejected"
    assert run.result.report["rejected"] == ["A person rejected the plan."]
    assert {step["status"] for step in run.result.steps} == {"dropped"}
    assert len(chat.calls) == 1  # no review of a plan that never ran


def test_a_confirmation_nobody_gave_in_time_ends_the_task_as_expired():
    chat = ScriptedChat(text(a_plan(studio_step())))

    run = Run(chat, confirmations=["expired"])

    assert run.result.outcome == "plan_expired"


def test_a_review_changes_the_plan_within_its_scope_after_a_rejected_post():
    first = a_plan(writing_step())
    changed = a_plan(writing_step(), writing_step("post-2"))
    chat = ScriptedChat(text(first), a_review("change_plan", changed), a_review("finish"))

    run = Run(
        chat,
        request="Write a post announcing offline sync.",
        children={
            RUN_WRITING: [
                writing_end("rejected"),
                writing_end(run_id=SECOND_WRITING_RUN_ID, item=SECOND_ITEM_ID),
            ]
        },
    )

    assert run.names() == [RUN_WRITING, RUN_WRITING]  # a change within scope needs no person
    assert run.result.plan_changes == 1
    [change] = run.result.plan_history
    assert TaskPlanChange.model_validate(change).replaced.steps[0].id == "post"
    assert change["reason"] == "Done."
    assert run.result.report["content_item_ids"] == [ITEM_ID, SECOND_ITEM_ID]
    assert run.result.report["rejected"] == ["The post of step 'post' was rejected by a person."]
    assert run.kinds().count(progress.PLAN_CHECKED) == 2


def test_a_change_that_adds_a_studio_step_goes_back_to_confirmation():
    first = a_plan(writing_step())
    changed = a_plan(writing_step(), studio_step(), writing_step("post-2", asset_from_step="image"))
    chat = ScriptedChat(
        text(first), a_review("change_plan", changed), a_review("dispatch_next"), a_review()
    )

    run = Run(
        chat,
        request="Write a post announcing offline sync.",
        children={
            RUN_WRITING: [writing_end("rejected"), writing_end(item=SECOND_ITEM_ID)],
            RUN_STUDIO: [studio_end()],
        },
    )

    assert run.names() == [RUN_WRITING, CONFIRM_PLAN, RUN_STUDIO, RUN_WRITING]
    [confirmation] = run.payloads(CONFIRM_PLAN)
    assert confirmation["confirmation"] == 1
    assert run.payloads(RUN_WRITING)[1]["brief"]["asset"] == ASSET_ID


def test_a_change_that_rewrites_a_step_that_ran_is_refused():
    first = a_plan(writing_step())
    rewritten = writing_step()
    rewritten["request"]["variants"] = 3
    chat = ScriptedChat(
        text(first),
        a_review("change_plan", a_plan(rewritten)),
        a_review("change_plan", a_plan(rewritten)),
    )

    run = Run(chat, request="Write a post.", children={RUN_WRITING: [writing_end()]})

    told = chat.calls[2]["messages"][-1]["content"]
    assert "step 'post' has run" in told
    assert run.result.plan_changes == 0
    assert any("the review could not be read" in note for note in run.result.notes)


def test_a_change_past_the_scopes_plan_changes_is_refused():
    first = a_plan(writing_step(), max_plan_changes=0)
    changed = a_plan(writing_step(), writing_step("post-2"), max_plan_changes=0)
    chat = ScriptedChat(
        text(first), a_review("change_plan", changed), a_review("change_plan", changed)
    )

    run = Run(chat, request="Write a post.", children={RUN_WRITING: [writing_end("rejected")]})

    assert (
        "changed its plan 0 times and its scope allows 0"
        in chat.calls[2]["messages"][-1]["content"]
    )
    assert run.names() == [RUN_WRITING]


def test_a_plan_with_more_steps_than_its_scope_is_refused():
    crowded = a_plan(writing_step("a"), writing_step("b"), max_steps=1)
    chat = ScriptedChat(text(crowded), text(crowded))

    with pytest.raises(TaskError):
        Run(chat, request="Write two posts.")

    assert "the plan has 2 steps and its scope allows 1" in chat.calls[1]["messages"][-1]["content"]


def test_a_failed_studio_run_drops_the_post_that_waited_for_its_image():
    chat = ScriptedChat(
        text(a_plan(studio_step(), writing_step(asset_from_step="image"))), a_review("finish")
    )

    run = Run(chat, children={RUN_STUDIO: [DID_NOT_FINISH]})

    assert run.names() == [CONFIRM_PLAN, RUN_STUDIO]
    studio, post = run.result.steps
    assert (studio["status"], studio["error"]) == ("failed", DID_NOT_FINISH)
    assert post["status"] == "dropped"
    assert run.result.outcome == "incomplete"
    assert progress.STEP_DROPPED in run.kinds()


def test_the_deadline_passed_ends_the_task_at_its_review_without_asking_the_model():
    chat = ScriptedChat(text(a_plan(writing_step(), writing_step("post-2"))))

    run = Run(
        chat,
        request="Write two posts.",
        children={RUN_WRITING: [writing_end()]},
        now=STARTED + timedelta(days=8),
    )

    assert run.names() == [RUN_WRITING]
    assert len(chat.calls) == 1
    assert any("deadline passed" in note for note in run.result.notes)
    assert run.result.steps[1]["status"] == "dropped"


def test_the_cost_limit_ends_the_task_before_a_plan():
    with pytest.raises(TaskError, match="cost limit"):
        Run(ScriptedChat(), cost_usd=1.0)


def test_the_cost_limit_of_the_scope_ends_the_task_at_its_review():
    chat = ScriptedChat(
        text(a_plan(writing_step(), writing_step("post-2"), max_cost_usd=0.5), cost_usd=0.6)
    )

    run = Run(chat, request="Write two posts.", children={RUN_WRITING: [writing_end()]})

    assert run.names() == [RUN_WRITING]
    assert any("cost limit" in note for note in run.result.notes)


def test_a_cut_off_plan_twice_is_worded_as_a_length_limit():
    cut = text("", finish_reason="length")
    chat = ScriptedChat(cut, cut)

    with pytest.raises(TaskError, match="max_tokens"):
        Run(chat)

    assert "cut off by the model's length limit" in chat.calls[1]["messages"][-1]["content"]


def test_a_weak_models_plan_as_json_text_and_an_agent_by_its_synonym_is_read():
    plan = a_plan(writing_step())
    plan["steps"][0]["agent"] = "Writer"
    plan["steps"][0]["request"] = json.dumps(plan["steps"][0]["request"])
    weak = {"plan": json.dumps({**plan, "steps": json.dumps(plan["steps"])})}
    chat = ScriptedChat(text(weak), a_review("finish"))

    run = Run(chat, request="Write a post.", children={RUN_WRITING: [writing_end()]})

    assert run.names() == [RUN_WRITING]


def test_the_orchestrator_is_sent_no_tools_and_reads_the_project_as_data():
    chat = ScriptedChat(text(a_plan(writing_step())), a_review("finish"))

    Run(chat, request="Write a post.", children={RUN_WRITING: [writing_end()]})

    assert all(call["tools"] is None and call["role"] == "orchestrator" for call in chat.calls)
    project = data_of(chat.calls[0]["messages"])["project"]
    assert project["live_features"] == [
        {"id": "offline-sync", "description": "Notes sync when the device is back online."}
    ]
    assert "src/sync.ts" not in json.dumps(chat.calls[0]["messages"])


def test_the_progress_shows_codes_records_only():
    chat = ScriptedChat(
        text(a_plan(studio_step(), writing_step(asset_from_step="image"))),
        a_review("dispatch_next"),
        a_review("finish"),
    )

    run = Run(chat, children={RUN_STUDIO: [studio_end()], RUN_WRITING: [writing_end()]})

    assert run.kinds() == [
        progress.TASK_PLANNED,
        progress.PLAN_CHECKED,
        progress.PLAN_CONFIRMATION,
        progress.STEP_FINISHED,
        progress.TASK_REVIEWED,
        progress.STEP_FINISHED,
        progress.TASK_REVIEWED,
        progress.TASK_REPORTED,
    ]
    finished = [event.detail for event in run.events if event.kind == progress.STEP_FINISHED]
    assert finished[1] == {
        "step": 2,
        "agent": "writing",
        "run_id": WRITING_RUN_ID,
        "status": "succeeded",
        "post_status": "dry_run",
    }
