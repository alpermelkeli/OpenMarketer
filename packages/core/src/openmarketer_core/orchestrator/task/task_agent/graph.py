"""The task's graph: its nodes, what follows what, and how one attempt asks the model.

This module is wiring only. The rules live in ``rules.py``, model access goes through
``ChatModel`` (the model router), and the four actions reach the world only through the
workflow that runs them: ``confirm_plan`` after the policy gate, which holds it for a person,
and the three run actions as child workflows the task waits for with no graph running.

    prepare -> plan -> check -> confirm -> dispatch -> review -> report -> END
                         ^  \\____________^  ^    |       |  \\_____^
                         |                   +----|-------+  (dispatch next)
                         +------------------------+          (change the plan)

``prepare`` keeps what the approved profile says of the project. ``plan`` has the orchestrator
write the plan, read strictly and held to the rules, asked once more when it does not fit.
``check`` adopts the submitted plan (the first, or a review's change), records its steps, and
decides whether a person confirms it; ``confirm`` asks for ``confirm_plan`` and ends the task
when the person rejects it or nobody answers. ``dispatch`` runs the next step whose waits all
succeeded, its references resolved by code, as one run action, and keeps a summary of what
the run gave. ``review`` has the orchestrator decide what follows: the next step, a change of
plan (back to ``check``, and to ``confirm`` when it adds or changes a studio step or raises
the scope) or the report. ``report`` builds the report from the steps' outcomes.

Steps run one at a time: a node asks for one action, and a graph waits for one, so "every step
whose dependencies are met" is the first of them, then the next after the review.

What each node shows of the task's progress is ``progress.py``'s, declared on the agent here.

Every node is safe to run twice. ``plan`` and ``review`` cost one or two model calls each.
``confirm`` and ``dispatch`` ask for the same action whenever they run, built from the state
alone, and do nothing before it.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel

from openmarketer_core.agent_runtime.agent import END
from openmarketer_core.agent_runtime.langgraph_steps import request_action
from openmarketer_core.agent_runtime.model_input import (
    DoesNotFit,
    answer_object,
    read_strict_json,
)
from openmarketer_core.llm import ChatReply
from openmarketer_core.orchestrator.task.failures import TaskError
from openmarketer_core.orchestrator.task.plan import (
    MAX_TASK_PLAN_CHANGES,
    MAX_TASK_STEPS,
    TaskPlan,
)
from openmarketer_core.orchestrator.task.task_agent import progress, rules
from openmarketer_core.orchestrator.task.task_agent.definitions import (
    ConfirmPlan,
    RunResearch,
    RunStudio,
    RunWriting,
    TaskContext,
    TaskState,
    agent,
)
from openmarketer_core.orchestrator.task.task_agent.rules import (
    ReferenceNotResolved,
    Review,
    ReviewDecision,
    StepStatus,
    TaskLimits,
)
from openmarketer_core.research.questions import ResearchRequest
from openmarketer_core.studio.requests import AssetRequest
from openmarketer_core.writing.brief import WritingBrief

logger = logging.getLogger(__name__)
# What a refusal in the log is said to be of.
AGENT_NAME = "task"

DISPATCH = "dispatch"
CHECK = "check"
CONFIRM = "confirm"
REPORT = "report"


@dataclass
class _Spent:
    """What the model calls of one node cost, the model that answered last, and whether its
    last answer was cut off by the role's length limit."""

    cost_usd: float = 0.0
    model: str = ""
    cut_off: bool = False

    def add(self, reply: ChatReply) -> None:
        self.cost_usd += reply.cost_usd
        self.model = reply.model or self.model
        self.cut_off = reply.finish_reason == "length"

    def unread(self, what: str) -> str:
        """Why ``what`` could not be read, worded for the person who reads the task."""
        if self.cut_off:
            cut = "the answer was cut off by the model's length limit, twice"
            return f"{what}: {cut}; {rules.CUT_OFF_HINT}"
        return f"{what} could not be read after two attempts"

    def update(self, state: TaskState) -> dict[str, Any]:
        return {
            "cost_usd": state["cost_usd"] + self.cost_usd,
            "model": self.model or state["model"],
        }


def _ask[Model: BaseModel](
    context: TaskContext,
    messages: list[dict[str, Any]],
    *,
    model_type: type[Model],
    shaped: Any,
    problems: Any,
    spent: _Spent,
) -> Model | None:
    """Ask the orchestrator for one JSON object, at most twice; ``None`` if it was not read.

    ``shaped`` reads a weak model's shapes as what they mean before the strict reading, and
    ``problems`` gives what the code's rules find wrong with what was read.
    """
    for _ in range(rules.SUBMISSION_ATTEMPTS):
        reply = context.model.chat(rules.ROLE, messages)
        spent.add(reply)
        answer = shaped(answer_object(reply.text))
        told: str
        if not isinstance(answer, dict):
            _log_refusal(model_type.__name__, "the answer was not one JSON object")
            told = "error: answer with one JSON object and nothing else"
        else:
            try:
                submitted = read_strict_json(model_type, answer)
            except DoesNotFit as e:
                _log_refusal(model_type.__name__, str(e))
                told = f"Rejected. Fix this and answer again: {e}"
            else:
                found = problems(submitted)
                if not found:
                    return submitted
                # A finding may quote the model's text: the log counts them.
                _log_refusal(model_type.__name__, f"{len(found)} finding(s) of the code rules")
                told = rules.problems_text(found)
        if reply.finish_reason == "length":
            told = f"The answer was cut off by the model's length limit. {told}"
        messages = [*messages, reply.message, {"role": "user", "content": told}]
    return None


def _log_refusal(submission: str, refusal: str) -> None:
    """Log a refused submission in code's own words, never with what the model wrote."""
    logger.warning("%s: %s refused: %s", AGENT_NAME, submission, refusal)


def _limits(state: TaskState) -> TaskLimits:
    return TaskLimits.of(state["limits"])


def _plan(state: TaskState) -> TaskPlan:
    return TaskPlan.model_validate(state["plan"])


def _deadline(state: TaskState, plan: TaskPlan) -> datetime:
    started = datetime.fromisoformat(state["started_at"])
    return started + timedelta(days=plan.scope.deadline_days)


# --------------------------------------------------------------------------------- nodes
@agent.node(start=True)
def prepare(state: TaskState, context: TaskContext) -> dict[str, Any]:
    """Keep what the task found at the project, for the plan and every review."""
    return {"at_hand": rules.at_hand_data(context.at_hand())}


@agent.node
def plan(state: TaskState, context: TaskContext) -> dict[str, Any]:
    """Have the orchestrator plan the task, read strictly and held to the rules."""
    limits = _limits(state)
    if state["cost_usd"] >= limits.max_cost_usd:
        raise TaskError(f"the cost limit (${limits.max_cost_usd:.2f}) was hit before a plan")
    request = state["request"]
    spent = _Spent()
    submitted = _ask(
        context,
        rules.plan_messages(request, state["at_hand"], limits),
        model_type=TaskPlan,
        shaped=rules.plan_data,
        problems=lambda proposed: rules.plan_problems(
            proposed,
            request=request["request"],
            project=state["at_hand"]["project"],
            reports=state["at_hand"]["research_reports"],
        ),
        spent=spent,
    )
    if submitted is None:
        raise TaskError(spent.unread("the orchestrator's plan"))
    return {"proposed_plan": submitted.model_dump(mode="json"), **spent.update(state)}


@agent.node
def check(state: TaskState) -> dict[str, Any]:
    """Adopt the submitted plan: its steps, whether a person confirms it, and what runs next.

    The plan was held to the same rules where it was submitted, so a plan that breaks them
    here is a defect of the code, and the task ends.
    """
    proposed = TaskPlan.model_validate(state["proposed_plan"])
    accepted = state["plan"] or None
    problems = rules.plan_problems(
        proposed,
        request=state["request"]["request"],
        project=state["at_hand"]["project"],
        reports=state["at_hand"]["research_reports"],
        steps=state["steps"],
        accepted=accepted,
        plan_changes=state["plan_changes"],
    )
    if problems:
        raise TaskError("the plan does not keep to the rules of a task: " + "; ".join(problems))
    needs = rules.needs_confirmation(proposed, limits=_limits(state), accepted=accepted)
    steps, notes = rules.with_dropped(proposed, rules.steps_of(proposed, state["steps"]))
    following = CONFIRM if needs else _dispatch_or_report(proposed, steps)
    history = state["plan_history"]
    if accepted:
        reason = state["review"].get("reason") or rules.CHANGE_WITHOUT_REASON
        history = [*history, {"reason": reason, "replaced": accepted}]
    return {
        "plan": proposed.model_dump(mode="json"),
        "proposed_plan": {},
        "plan_changes": state["plan_changes"] + (1 if accepted else 0),
        "plan_history": history,
        "needs_confirmation": needs,
        "steps": steps,
        "next": following,
        "notes": [*state["notes"], *notes],
    }


@agent.node
def confirm(state: TaskState) -> dict[str, Any]:
    """Ask a person to confirm the plan (``confirm_plan``); a rejection ends the task."""
    confirmation = state["confirmations"] + 1
    outcome = request_action(
        ConfirmPlan(
            task_id=uuid.UUID(state["run_id"]), confirmation=confirmation, plan=_plan(state)
        )
    )
    status = rules.confirmation_status(outcome.succeeded, outcome.error)
    following = REPORT
    if status == "confirmed":
        following = _dispatch_or_report(_plan(state), state["steps"])
    return {
        "confirmations": confirmation,
        "confirmation": status,
        "needs_confirmation": False,
        "next": following,
    }


@agent.node
def dispatch(state: TaskState) -> dict[str, Any]:
    """Run the next step whose waits succeeded, and keep what its run gave."""
    current = _plan(state)
    index = rules.next_step(current, state["steps"])
    if index is None:  # the routes come here only when a step is ready
        raise TaskError("the task had no step to run")
    step = current.steps[index]
    steps = [dict(record) for record in state["steps"]]
    record = next(record for record in steps if record["id"] == step.id)
    try:
        asked = rules.request_of(step, state["steps"], state["at_hand"]["research_reports"])
    except ReferenceNotResolved as e:
        record.update(status=StepStatus.FAILED.value, error=str(e))
    else:
        outcome = request_action(_payload(state, step.id, asked))
        if outcome.succeeded:
            record.update(
                status=StepStatus.SUCCEEDED.value,
                run_id=str(outcome.result.get("run_id") or ""),
                outcome=rules.step_outcome(step.agent, outcome.result),
            )
        else:
            record.update(status=StepStatus.FAILED.value, error=outcome.error)
    steps, notes = rules.with_dropped(current, steps)
    return {"steps": steps, "finished_step": step.id, "notes": [*state["notes"], *notes]}


def _payload(
    state: TaskState, step_id: str, asked: ResearchRequest | AssetRequest | WritingBrief
) -> BaseModel:
    task_id = uuid.UUID(state["run_id"])
    if isinstance(asked, ResearchRequest):
        return RunResearch(task_id=task_id, step=step_id, request=asked)
    if isinstance(asked, AssetRequest):
        return RunStudio(task_id=task_id, step=step_id, request=asked)
    return RunWriting(task_id=task_id, step=step_id, brief=asked)


@agent.node
def review(state: TaskState, context: TaskContext) -> dict[str, Any]:
    """Have the orchestrator decide what follows the step that finished.

    The deadline and the cost cap of the plan's scope are held first, by code: past either,
    the task goes to its report without asking. A review that cannot be read goes on with the
    plan the person confirmed.
    """
    current = _plan(state)
    scope = current.scope
    now = context.now()
    deadline = _deadline(state, current)
    if now >= deadline:
        return _ended_by(state, "the task's deadline passed")
    if state["cost_usd"] >= float(scope.max_cost_usd):
        return _ended_by(state, f"the task's cost limit (${scope.max_cost_usd}) was hit")
    left = rules.Left(
        steps=scope.max_steps - len(state["steps"]),
        plan_changes=scope.max_plan_changes - state["plan_changes"],
        days=(deadline - now).total_seconds() / 86400,
        cost_usd=float(scope.max_cost_usd) - state["cost_usd"],
    )
    spent = _Spent()
    submitted = _ask(
        context,
        rules.review_messages(
            state["request"],
            state["at_hand"],
            state["plan"],
            state["steps"],
            state["finished_step"],
            left,
        ),
        model_type=Review,
        shaped=rules.review_data,
        problems=lambda decided: _change_problems(state, decided),
        spent=spent,
    )
    if submitted is None:
        note = spent.unread("the review") + "; the plan went on as it was"
        return {
            "review": {},
            "next": _dispatch_or_report(current, state["steps"]),
            "notes": [*state["notes"], note],
            **spent.update(state),
        }
    changes: dict[str, Any] = {"review": submitted.model_dump(mode="json"), **spent.update(state)}
    if submitted.decision is ReviewDecision.CHANGE_PLAN and submitted.plan is not None:
        return {**changes, "proposed_plan": submitted.plan.model_dump(mode="json"), "next": CHECK}
    if submitted.decision is ReviewDecision.DISPATCH_NEXT:
        return {**changes, "next": _dispatch_or_report(current, state["steps"])}
    return {**changes, "next": REPORT}


def _change_problems(state: TaskState, decided: Review) -> list[str]:
    if decided.plan is None:
        return []
    return rules.plan_problems(
        decided.plan,
        request=state["request"]["request"],
        project=state["at_hand"]["project"],
        reports=state["at_hand"]["research_reports"],
        steps=state["steps"],
        accepted=state["plan"],
        plan_changes=state["plan_changes"],
    )


def _ended_by(state: TaskState, why: str) -> dict[str, Any]:
    return {"review": {}, "next": REPORT, "notes": [*state["notes"], f"{why}: the task ended"]}


def _dispatch_or_report(current: TaskPlan, steps: list[dict[str, Any]]) -> str:
    return DISPATCH if rules.next_step(current, steps) is not None else REPORT


@agent.node
def report(state: TaskState) -> dict[str, Any]:
    """Build the report from the steps' outcomes; a step that never ran is dropped."""
    steps = [
        record
        if record["status"] != StepStatus.PENDING
        else {**record, "status": StepStatus.DROPPED.value, "error": "the task ended before it"}
        for record in state["steps"]
    ]
    reported = rules.task_report(
        state["plan"],
        steps,
        confirmation=state["confirmation"],
        own_cost_usd=state["cost_usd"],
        notes=state["notes"],
    )
    return {
        "steps": steps,
        "outcome": reported.outcome.value,
        "report": reported.report,
        "total_cost_usd": reported.cost_usd,
    }


agent.progress(progress.progress_of)
agent.edge(prepare, plan)
agent.edge(plan, check)
agent.edge(dispatch, review)
agent.edge(report, END)


@agent.route(after=check, to=[confirm, dispatch, report])
def after_check(state: TaskState) -> str:
    return state["next"]


@agent.route(after=confirm, to=[dispatch, report])
def after_confirm(state: TaskState) -> str:
    return state["next"]


@agent.route(after=review, to=[dispatch, check, report])
def after_review(state: TaskState) -> str:
    return state["next"]


def recursion_limit() -> int:
    """How many graph steps a task may take before it is stopped as a defect.

    prepare, plan, check and confirm; then for each run a step may start and each change of
    plan, a dispatch, a review, a check and a confirmation; and the report.
    """
    return 4 + 4 * (MAX_TASK_STEPS + MAX_TASK_PLAN_CHANGES) + 1 + 10
