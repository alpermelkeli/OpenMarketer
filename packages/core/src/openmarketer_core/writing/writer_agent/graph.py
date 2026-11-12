"""The writer's graph: its nodes, what follows what, and how one attempt asks the models.

This module is wiring only. The rules live in ``rules.py``, model access goes through
``ChatModel`` (the model router), and the two actions (``store_draft``, ``publish_post``)
reach the database and the world only through the workflow that runs them, the second after
the policy gate. The state holds plain data only.

    prepare -> write -> check -> decide -> store -> target -> propose -> record -> END
                         ^          |
                         +- revise -+

``prepare`` reads the project's approved profile, keeps its version's id and builds the
writing context from it, with the asset and the research run the brief names, read through the
context and kept as bounded data; a project without a profile, a brief that announces a feature
that is not live, or an asset or a research run that cannot be used, ends the run here.
``write`` has the writer write the variants, each with an angle and a form of its own, in one
call without tools. ``check`` runs the code rules on every variant, the run's only review;
``decide`` applies ``rules.decide``, and ends the run when nothing can be proposed; ``revise``
has the writer rewrite from the code's findings, for another round. ``store`` asks for
``store_draft``, which stores the draft the person will review as the run's content item, and
keeps the item's id; ``target`` reads the project's connected account on the post's
platform and what a post is reckoned to cost, and keeps them; ``propose`` asks for
``publish_post`` with that id, the text, the image's version and the target: the gate holds it
until a person decides, and a rejection or an expiry comes back as the outcome. Each action is
asked for by a node of its own. ``record`` sets the result, which whoever runs the graph stores.

The writer has no tools: it answers with one JSON object, read strictly. An answer that does
not fit is told what was wrong and asked once more, inside the node.

What each node shows of the run's progress is ``progress.py``'s, declared on the agent here.

Every node is safe to run twice: a step that died before its checkpoint is taken again from
the same state, and the model calls are made for this attempt only. ``store`` and ``propose``
each ask for the same action whenever they run, built from the state alone, and do nothing
before it. That is why the account is read by ``target``, a node of its own before
``propose``: read by ``propose``, an account connected or replaced while the post waits for a
person would change what ``propose`` asks for when it runs again, and the runtime refuses a
request that changed. A thread stored before ``target`` existed and waiting in ``store``
passes through it; one already waiting in ``propose`` has no target and asks for the dry run
it asked for before.
"""

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
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
    Draft,
    FeatureNotPublishable,
    NoApprovedProfile,
    NoVariantToPropose,
)

logger = logging.getLogger(__name__)
# What a refusal in the log is said to be of.
AGENT_NAME = "writer"

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
            cut = "the answer was cut off by the model's length limit, twice"
            return f"{what}: {cut}; {rules.CUT_OFF_HINT}"
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
        _log_refusal(model_type.__name__, "the answer was not one JSON object")
        return "error: answer with one JSON object and nothing else"
    try:
        submitted = read_strict_json(model_type, rules.unwrapped_arguments(model_type, answer))
    except DoesNotFit as e:
        _log_refusal(model_type.__name__, str(e))
        return f"Rejected. Fix this and answer again: {e}"
    found = problems(submitted)
    if found:
        # A finding may quote the model's text: the log counts them.
        _log_refusal(model_type.__name__, f"{len(found)} finding(s) of the code rules")
        return "Rejected. Fix this and answer again: " + "; ".join(found)
    return submitted


def _log_refusal(submission: str, refusal: str) -> None:
    """Log a refused submission in code's own words, never with what the model wrote."""
    logger.warning("%s: %s refused: %s", AGENT_NAME, submission, refusal)


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
        submitted = _read(answer_object(reply.text), model_type, problems)
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
    """Keep the approved profile's version and build the writing context from it, with the
    brief's asset and research when it names them.

    Raises ``PostImageNotUsable`` or ``ResearchNotUsable`` (the context's readings raise them)
    when what the brief names cannot be used any more: the request checked it, and a person may
    have rejected the asset since.
    """
    brief = _brief(state)
    approved = context.approved_profile()
    if approved is None:
        raise NoApprovedProfile()
    problem = rules.announced_feature_problem(brief, approved.profile)
    if problem is not None:
        raise FeatureNotPublishable(problem)
    writing_context = rules.writing_context(approved.profile)
    notes = state["notes"]
    if brief.asset is not None:
        writing_context["image"] = rules.image_context(context.post_image(brief.asset))
    if brief.research_run_id is not None:
        research = rules.research_context(context.research_findings(brief.research_run_id))
        writing_context["research"] = research.research
        notes = [*notes, *research.notes]
    return {
        "profile_version_id": str(approved.version_id),
        "writing_context": writing_context,
        "notes": notes,
    }


@agent.node
def write(state: WriterState, context: WriterContext) -> dict[str, Any]:
    """Have the writer write the first round's variants."""
    _cost_left(state, context, "before the post was written")
    brief = _brief(state)
    spent = _Spent()
    submitted = _ask(
        context,
        rules.WRITER_ROLE,
        rules.write_messages(brief, state["writing_context"]),
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
def decide(state: WriterState, context: WriterContext) -> dict[str, Any]:
    """Apply the decide rule to the round: propose a variant, revise, or end the run."""
    decision = rules.decide(
        checks=state["checks"],
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
    }


@agent.node
def revise(state: WriterState, context: WriterContext) -> dict[str, Any]:
    """Have the writer rewrite the variants from the code's findings, for the next round.

    A rewrite that cannot be read keeps the round's variants for the next, with a note: they
    fail as they did, and the round limit ends the run.
    """
    brief = _brief(state)
    round_no = state["round"]
    finished = {
        "round": round_no,
        "variants": state["variants"],
        "checks": state["checks"],
    }
    spent = _Spent()
    submitted = _ask(
        context,
        rules.WRITER_ROLE,
        rules.revise_messages(
            brief,
            state["writing_context"],
            round_no=round_no,
            variants=state["variants"],
            checks=state["checks"],
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
def target(state: WriterState, context: WriterContext) -> dict[str, Any]:
    """Keep where the post would be published: the project's connected account on the post's
    platform and what a post is reckoned to cost, or nothing for a dry run."""
    platform = rules.social_platform(_brief(state).platform)
    if platform is None:
        return {"target": {}}
    account = context.publishing_account(platform)
    with_image = bool(state["draft"].get("asset_version_id"))
    estimate = context.publishing_costs.estimate(platform, with_image=with_image)
    return {"target": rules.publishing_target(account, estimate)}


@agent.node
def propose(state: WriterState, context: WriterContext) -> dict[str, Any]:
    """Ask for ``publish_post``: the gate holds it as a proposal until a person decides."""
    asset_version_id = state["draft"].get("asset_version_id")
    # A thread stored before ``target`` existed has none, and asks for the dry run it asked for.
    kept = state.get("target") or {}
    estimate = kept.get("cost_estimate_usd")
    outcome = request_action(
        PublishPost(
            **_run_ids(state),
            content_item_id=uuid.UUID(state["content_item_id"]),
            text=state["draft"]["text"],
            asset_version_id=uuid.UUID(asset_version_id) if asset_version_id else None,
            account_id=kept.get("account_id"),
            username=kept.get("username"),
            cost_estimate_usd=Decimal(estimate) if estimate is not None else None,
        )
    )
    status = rules.post_status(outcome.succeeded, outcome.error, outcome.result)
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
        # No critic reviews the post; a draft stored before carries its report all the same.
        "passed_critic": None,
        "reason": state["reason"],
        "action_error": state["action_error"],
        "rounds": state["round"],
        "cost_usd": state["cost_usd"],
        "model": state["model"],
        "notes": state["notes"],
        # A draft stored before the brief had inputs names none.
        "asset_id": draft.get("asset_id"),
        "asset_version_id": draft.get("asset_version_id"),
        "research_run_id": draft.get("research_run_id"),
    }
    return {"result": result}


agent.progress(progress.progress_of)
agent.edge(prepare, write)
agent.edge(write, check)
agent.edge(check, decide)
agent.edge(revise, check)
agent.edge(store, target)
agent.edge(target, propose)
agent.edge(propose, record)
agent.edge(record, END)


@agent.route(after=decide, to=[store, revise])
def after_decide(state: WriterState) -> str:
    return store.__name__ if state["decision"] == "propose" else revise.__name__


def recursion_limit(limits: rules.Limits) -> int:
    """How many graph steps a run under ``limits`` may take before it is stopped as a defect."""
    # prepare and write; check, decide and revise per round; store, target, propose and record
    return 2 + 3 * limits.max_rounds + 4 + 10
