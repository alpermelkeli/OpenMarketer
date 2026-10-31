"""The writer's graph: its nodes, what follows what, and how one attempt asks the models.

This module is wiring only. The rules live in ``rules.py``, model access goes through
``ChatModel`` (the model router), and the two actions (``store_draft``, ``publish_post``)
reach the database and the world only through the workflow that runs them, the second after
the policy gate. The state holds plain data only.

    prepare -> plan -> draft -> check -> critique -> decide -> store -> propose -> record -> END
                                  ^                    |
                                  +----- revise <------+

``prepare`` reads the project's approved profile, keeps its version's id and builds the
writing context from it; a project without one, or a brief that announces a feature that is
not live, ends the run here. ``plan`` has the writer plan the post, which code holds to the
brief and the project; ``draft`` has it write the variants, without tools. ``check`` runs the
code rules on every variant; ``critique`` has the critic, another role, review those that kept
them; ``decide`` applies ``rules.decide``, and ends the run when nothing can be proposed;
``revise`` has the writer rewrite from the findings, for another round. ``store`` asks for
``store_draft``, which stores the draft the person will review as the run's content item, and
keeps the item's id; ``propose`` asks for ``publish_post`` with that id and the text: the gate
holds it until a person decides, and a rejection or an expiry comes back as the outcome. Each
action is asked for by a node of its own. ``record`` sets the result, which whoever runs the
graph stores.

The writer and the critic have no tools: each answers with one JSON object, read strictly.
An answer that does not fit is told what was wrong and asked once more, inside the node.

What each node shows of the run's progress is ``progress.py``'s, declared on the agent here.

Every node is safe to run twice: a step that died before its checkpoint is taken again from
the same state, and the model calls are made for this attempt only. ``store`` and ``propose``
each ask for the same action whenever they run, built from the state alone, and do nothing
before it.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from openmarketer_core.agent_runtime.agent import END
from openmarketer_core.agent_runtime.langgraph_steps import request_action
from openmarketer_core.agent_runtime.model_input import DoesNotFit, read_strict_json
from openmarketer_core.llm import ChatReply
from openmarketer_core.writing.brief import WritingBrief
from openmarketer_core.writing.failures import WritingError
from openmarketer_core.writing.writer_agent import progress, rules
from openmarketer_core.writing.writer_agent.definitions import (
    PublishPost,
    StoreDraft,
    WriterContext,
    WriterState,
    agent,
)
from openmarketer_core.writing.writer_agent.rules import (
    CriticReport,
    Draft,
    FeatureNotPublishable,
    NoApprovedProfile,
    NoVariantToPropose,
    Plan,
)

Problems = Callable[[Any], list[str]]


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
        """Why ``what`` could not be read, worded for the person who reads the run."""
        if self.cut_off:
            return f"{what} was cut off by the model's length limit, twice: {rules.CUT_OFF_HINT}"
        return f"{what} could not be read after two attempts"

    def update(self, state: WriterState) -> dict[str, Any]:
        return {
            "cost_usd": state["cost_usd"] + self.cost_usd,
            "model": self.model or state["model"],
        }


def _no_problems(submitted: Any) -> list[str]:
    return []


def _read[Model: BaseModel](
    answer: Any, model_type: type[Model], problems: Problems
) -> Model | str:
    """The submission in ``answer``, or the text that tells the model what was wrong."""
    if not isinstance(answer, dict):
        return "error: answer with one JSON object and nothing else"
    try:
        submitted = read_strict_json(model_type, rules.unwrapped_arguments(model_type, answer))
    except DoesNotFit as e:
        return f"Rejected. Fix this and answer again: {e}"
    found = problems(submitted)
    if found:
        return "Rejected. Fix this and answer again: " + "; ".join(found)
    return submitted


def _ask[Model: BaseModel](
    context: WriterContext,
    role: str,
    messages: list[dict[str, Any]],
    *,
    model_type: type[Model],
    spent: _Spent,
    problems: Problems = _no_problems,
) -> Model | None:
    """Ask a role that is sent no tools for one JSON object, at most twice; ``None`` if unread.

    An answer cut off by the model's length limit is told so: a model that spends its budget on
    reasoning writes nothing, which is otherwise told apart from a refusal by nobody.
    """
    for _ in range(rules.SUBMISSION_ATTEMPTS):
        reply = context.model.chat(role, messages)
        spent.add(reply)
        submitted = _read(rules.answer_object(reply.text), model_type, problems)
        if not isinstance(submitted, str):
            return submitted
        if reply.finish_reason == "length":
            submitted = f"The answer was cut off by the model's length limit. {submitted}"
        messages = [*messages, reply.message, {"role": "user", "content": submitted}]
    return None


def _brief(state: WriterState) -> WritingBrief:
    return WritingBrief.model_validate(state["brief"])


def _cost_left(state: WriterState, context: WriterContext, before: str) -> None:
    if state["cost_usd"] >= context.limits.max_cost_usd:
        raise WritingError(f"the cost limit (${context.limits.max_cost_usd:.2f}) was hit {before}")


def _variants_of(
    draft: Draft, wanted: int, round_no: int
) -> tuple[list[dict[str, Any]], list[str]]:
    """The variants a round keeps: as many as the brief asked for, with a note of any dropped."""
    variants = [variant.model_dump(mode="json") for variant in draft.variants]
    notes: list[str] = []
    if len(variants) > wanted:
        notes.append(f"round {round_no}: kept the first {wanted} of {len(variants)} variants")
        variants = variants[:wanted]
    elif len(variants) < wanted:
        notes.append(f"round {round_no}: the writer wrote {len(variants)} of {wanted} variants")
    return variants, notes


# --------------------------------------------------------------------------------- nodes
@agent.node(start=True)
def prepare(state: WriterState, context: WriterContext) -> dict[str, Any]:
    """Keep the approved profile's version and build the writing context from it."""
    approved = context.approved_profile()
    if approved is None:
        raise NoApprovedProfile()
    problem = rules.announced_feature_problem(_brief(state), approved.profile)
    if problem is not None:
        raise FeatureNotPublishable(problem)
    return {
        "profile_version_id": str(approved.version_id),
        "writing_context": rules.writing_context(approved.profile),
    }


@agent.node
def plan(state: WriterState, context: WriterContext) -> dict[str, Any]:
    """Have the writer plan the post, and hold the plan to the brief and the project."""
    _cost_left(state, context, "before the post was planned")
    brief = _brief(state)
    spent = _Spent()
    submitted = _ask(
        context,
        rules.WRITER_ROLE,
        rules.plan_messages(brief, state["writing_context"]),
        model_type=Plan,
        spent=spent,
    )
    if submitted is None:
        raise WritingError(spent.unread("the writer's plan"))
    held = rules.held_plan(submitted, brief, state["writing_context"])
    return {"plan": held.plan, "plan_changes": held.changes, **spent.update(state)}


@agent.node
def draft(state: WriterState, context: WriterContext) -> dict[str, Any]:
    """Have the writer write the first round's variants from the plan."""
    _cost_left(state, context, "before the post was written")
    brief = _brief(state)
    spent = _Spent()
    submitted = _ask(
        context,
        rules.WRITER_ROLE,
        rules.draft_messages(brief, state["writing_context"], state["plan"]),
        model_type=Draft,
        spent=spent,
    )
    if submitted is None:
        raise WritingError(spent.unread("the writer's variants"))
    variants, notes = _variants_of(submitted, brief.variants, 1)
    return {
        "round": 1,
        "variants": variants,
        "notes": [*state["notes"], *notes],
        **spent.update(state),
    }


@agent.node
def check(state: WriterState, context: WriterContext) -> dict[str, Any]:
    """Run the code rules on every variant of the round."""
    brief = _brief(state)
    situation = rules.situation(brief.platform, state["writing_context"])
    known = rules.profile_numbers(state["writing_context"])
    checks = [
        rules.check_variant(rules.Variant.model_validate(variant), situation, known)
        for variant in state["variants"]
    ]
    return {"checks": checks}


@agent.node
def critique(state: WriterState, context: WriterContext) -> dict[str, Any]:
    """Have the critic review the variants that kept every code rule, and rank those it passes.

    Nothing is asked when no variant kept them. When the run's cost is used up, or the critic's
    report cannot be read, the round has no review, and ``decide`` proposes nothing of it.
    """
    sent = [index for index, check in enumerate(state["checks"]) if check["passed"]]
    if not sent:
        return {"critique": {}}
    if state["cost_usd"] >= context.limits.max_cost_usd:
        note = f"round {state['round']}: the critic was not asked, the cost limit was hit"
        return {"critique": {}, "notes": [*state["notes"], note]}
    brief = _brief(state)
    spent = _Spent()
    submitted = _ask(
        context,
        rules.CRITIC_ROLE,
        rules.critic_messages(
            brief,
            state["writing_context"],
            state["plan"],
            variants=state["variants"],
            checks=state["checks"],
            sent=sent,
        ),
        model_type=CriticReport,
        spent=spent,
        problems=lambda report: rules.critic_problems(report, sent),
    )
    if submitted is None:
        note = f"round {state['round']}: " + spent.unread("the critic's report")
        return {"critique": {}, "notes": [*state["notes"], note], **spent.update(state)}
    return {"critique": submitted.model_dump(mode="json"), **spent.update(state)}


@agent.node
def decide(state: WriterState, context: WriterContext) -> dict[str, Any]:
    """Apply the decide rule to the round: propose a variant, revise, or end the run."""
    decision = rules.decide(
        checks=state["checks"],
        critique=state["critique"],
        round_no=state["round"],
        cost_usd=state["cost_usd"],
        limits=context.limits,
    )
    if decision.next == "fail":
        raise NoVariantToPropose(decision.reason)
    return {
        "decision": decision.next,
        "reason": decision.reason,
        "chosen": decision.chosen,
        "passed_critic": decision.passed_critic,
    }


@agent.node
def revise(state: WriterState, context: WriterContext) -> dict[str, Any]:
    """Have the writer rewrite the variants from the round's findings, for the next round.

    A rewrite that cannot be read keeps the round's variants for the next, with a note: they
    fail as they did, and the round limit ends the run.
    """
    brief = _brief(state)
    round_no = state["round"]
    finished = {
        "round": round_no,
        "variants": state["variants"],
        "checks": state["checks"],
        "critique": state["critique"],
    }
    spent = _Spent()
    submitted = _ask(
        context,
        rules.WRITER_ROLE,
        rules.revise_messages(
            brief,
            state["writing_context"],
            state["plan"],
            round_no=round_no,
            variants=state["variants"],
            checks=state["checks"],
            critique=state["critique"],
        ),
        model_type=Draft,
        spent=spent,
    )
    if submitted is None:
        variants = state["variants"]
        notes = [f"round {round_no + 1}: {spent.unread('the rewrite')}; the variants are kept"]
    else:
        variants, notes = _variants_of(submitted, brief.variants, round_no + 1)
    return {
        "round": round_no + 1,
        "variants": variants,
        "checks": [],
        "critique": {},
        "earlier_rounds": [*state["earlier_rounds"], finished],
        "chosen": -1,
        "notes": [*state["notes"], *notes],
        **spent.update(state),
    }


def _run_ids(state: WriterState) -> dict[str, uuid.UUID]:
    return {
        "workspace_id": uuid.UUID(state["workspace_id"]),
        "project_id": uuid.UUID(state["project_id"]),
        "run_id": uuid.UUID(state["run_id"]),
    }


@agent.node
def store(state: WriterState, context: WriterContext) -> dict[str, Any]:
    """Ask for ``store_draft``: the draft the person will review, stored as the content item.

    The draft is built from the state alone, so it is the same whenever the node runs. The
    last alternatives are left out, with a note, when the draft would weigh more than an action
    may (``rules.fitted_draft``). A draft that could not be stored ends the run.
    """
    built = rules.draft_of(
        chosen=state["chosen"],
        variants=state["variants"],
        checks=state["checks"],
        critique=state["critique"],
        plan=state["plan"],
        plan_changes=state["plan_changes"],
        context=state["writing_context"],
        cost_usd=state["cost_usd"],
    )
    draft, left_out = rules.fitted_draft(built)
    outcome = request_action(StoreDraft.model_validate({**_run_ids(state), **draft}))
    if not outcome.succeeded:
        raise WritingError(f"the draft could not be stored: {outcome.error}")
    notes = state["notes"]
    if left_out:
        notes = [*notes, f"{left_out} alternatives were left out of the draft for its size"]
    return {
        "draft": draft,
        "content_item_id": str(uuid.UUID(str(outcome.result["content_item_id"]))),
        "notes": notes,
    }


@agent.node
def propose(state: WriterState, context: WriterContext) -> dict[str, Any]:
    """Ask for ``publish_post``: the gate holds it as a proposal until a person decides."""
    outcome = request_action(
        PublishPost(
            **_run_ids(state),
            content_item_id=uuid.UUID(state["content_item_id"]),
            text=state["draft"]["text"],
        )
    )
    status = rules.post_status(outcome.succeeded, outcome.error)
    return {"status": status.value, "action_error": outcome.error}


@agent.node
def record(state: WriterState) -> dict[str, Any]:
    """Set the result: the draft as it was stored, its content item, and how the post ended."""
    chosen = state["chosen"]
    draft = state["draft"]
    variant = state["variants"][chosen]
    result = {
        "status": state["status"],
        "profile_version_id": state["profile_version_id"],
        "platform": state["brief"]["platform"],
        "text": draft["text"],
        "language": variant["language"],
        "chosen": {
            "index": chosen,
            "variant": variant,
            "check": draft["check_report"],
            "review": draft["critic_report"],
        },
        "alternatives": draft["alternatives"],
        "plan": draft["plan"],
        "plan_changes": draft["plan_changes"],
        "claims": draft["claims"],
        "check_report": draft["check_report"],
        "critic_report": draft["critic_report"],
        "flagged_numbers": draft["flagged_numbers"],
        "content_item_id": state["content_item_id"],
        "passed_critic": state["passed_critic"],
        "reason": state["reason"],
        "action_error": state["action_error"],
        "rounds": state["round"],
        "cost_usd": state["cost_usd"],
        "model": state["model"],
        "notes": state["notes"],
    }
    return {"result": result}


agent.progress(progress.progress_of)
agent.edge(prepare, plan)
agent.edge(plan, draft)
agent.edge(draft, check)
agent.edge(check, critique)
agent.edge(critique, decide)
agent.edge(revise, check)
agent.edge(store, propose)
agent.edge(propose, record)
agent.edge(record, END)


@agent.route(after=decide, to=[store, revise])
def after_decide(state: WriterState) -> str:
    return store.__name__ if state["decision"] == "propose" else revise.__name__


def recursion_limit(limits: rules.Limits) -> int:
    """How many graph steps a run under ``limits`` may take before it is stopped as a defect."""
    # prepare, plan and draft; check, critique, decide and revise per round; store, propose
    # and record
    return 3 + 4 * limits.max_rounds + 3 + 10
