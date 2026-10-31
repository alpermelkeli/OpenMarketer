"""Tests for the writer's graph, run step by step with its one action answered here.

The two roles are scripted (``ScriptedChat``), the approved profile is a fixture, and the
action ``publish_post`` is answered as the runtime would answer it: approved and recorded as a
dry run, or not approved. The thread is kept in memory by the strict serializer the PostgreSQL
store uses, so a state that is not plain data fails here as it would in the worker. Nothing
reaches a network or a database.
"""

import json
import subprocess
import sys
import uuid
from collections.abc import Mapping
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from openmarketer_core.agent_runtime.actions import ActionOutcome
from openmarketer_core.agent_runtime.langgraph_agent import (
    ActionAskedInLocalRun,
    AgentSteps,
    run_from,
)
from openmarketer_core.agent_runtime.steps import GraphRunRef
from openmarketer_core.agent_runtime.threads import thread_id
from openmarketer_core.graph_checkpoints import RunCheckpoints
from openmarketer_core.llm import ChatReply
from openmarketer_core.profile import ProductProfile
from openmarketer_core.run_events import RunEvent
from openmarketer_core.writing.brief import WritingBrief, WritingGoal, WritingPlatform
from openmarketer_core.writing.failures import WRITING_FAILURES, WritingError
from openmarketer_core.writing.workflow_contract import WRITING_RUN_KIND, WritingRunInput
from openmarketer_core.writing.writer_agent import progress, rules
from openmarketer_core.writing.writer_agent.definitions import (
    PUBLISH_POST,
    STORE_DRAFT,
    ApprovedProfile,
    WriterContext,
    agent,
    initial_state,
    result_of,
)
from openmarketer_core.writing.writer_agent.graph import recursion_limit
from openmarketer_core.writing.writer_agent.rules import (
    FeatureNotPublishable,
    Limits,
    NoApprovedProfile,
    NoVariantToPropose,
    PostStatus,
)

VERSION_ID = uuid.UUID("77777777-7777-4777-8777-777777777777")
ITEM_ID = "88888888-8888-4888-8888-888888888888"


def a_profile(**changes: Any) -> ProductProfile:
    data: dict[str, Any] = {
        "product": {"name": "Acme Notes", "type": "b2b_saas", "languages": ["en"]},
        "features": [
            {
                "id": "offline-sync",
                "description": "Notes sync when the device is back online.",
                "status": "live",
                "evidence": [{"file": "src/sync.ts", "lines": "10-40"}],
                "confidence": 0.9,
            },
            {
                "id": "ai-summary",
                "description": "Summaries of long notes.",
                "status": "unreleased",
            },
        ],
        "brand": {"voice": "Plain and warm.", "do_not_mention": ["BetaCorp"]},
        "business_model": {"type": "subscription", "trial_days": 14},
    }
    data.update(changes)
    return ProductProfile.model_validate(data)


# ------------------------------------------------------------------------ the scripted models
class ScriptedChat:
    """Prepared answers per role, in order; each call records what it was sent."""

    def __init__(self, **answers: list[ChatReply]):
        self.answers = {role: list(queue) for role, queue in answers.items()}
        self.calls: list[dict[str, Any]] = []

    def chat(self, role, messages, *, tools=None, tool_choice=None) -> ChatReply:
        self.calls.append({"role": role, "messages": messages, "tools": tools})
        return self.answers[role].pop(0)

    def sent_to(self, role: str) -> list[list[dict[str, Any]]]:
        return [call["messages"] for call in self.calls if call["role"] == role]


def text(content: Any, *, cost_usd: float = 0.01) -> ChatReply:
    body = content if isinstance(content, str) else json.dumps(content)
    return ChatReply(
        message={"role": "assistant", "content": body}, model="vendor/m", cost_usd=cost_usd
    )


def a_plan(**changes: Any) -> ChatReply:
    plan = {"angle": "Write anywhere.", "feature_ids": ["offline-sync"], "format": "statement"}
    return text({**plan, **changes})


def a_variant(body: str = "Your notes keep up offline. Write anywhere.", **changes: Any):
    variant = {
        "text": body,
        "angle": "Freedom from connectivity.",
        "language": "en",
        "claims": [{"statement": "Notes sync after offline use.", "feature_ids": ["offline-sync"]}],
    }
    return {**variant, **changes}


def a_draft(*variants: dict[str, Any]) -> ChatReply:
    return text({"variants": list(variants) or [a_variant(), a_variant("Offline? Keep writing.")]})


def a_review(index: int, *, risky: bool = False, **failing: str) -> dict[str, Any]:
    return {
        "variant": index,
        "criteria": [
            {
                "criterion": criterion.value,
                "passed": criterion.value not in failing,
                "severity": failing.get(criterion.value, "none"),
                "note": "",
            }
            for criterion in rules.Criterion
        ],
        "risky_claim": risky,
        "risky_note": "promises a result" if risky else "",
        "suggestions": [],
    }


def a_report(*reviews: dict[str, Any], ranking: list[int] | None = None) -> ChatReply:
    return text({"reviews": list(reviews), "ranking": ranking or []})


def data_of(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """The data block of a conversation's user message."""
    content = messages[1]["content"]
    return json.loads(content.split("Data:\n", 1)[1])


# ------------------------------------------------------------------------------- the run
class Run:
    """One run of the graph to its end, its action answered by ``decision``."""

    def __init__(
        self,
        chat: ScriptedChat,
        *,
        brief: WritingBrief | None = None,
        profile: ProductProfile | None = None,
        approved: bool = True,
        decision: ActionOutcome | str = "approve",
        store_error: str = "",
        limits: Limits | None = None,
    ):
        self.brief = brief or WritingBrief(
            platform=WritingPlatform.X, goal=WritingGoal.INTRODUCE, variants=2
        )
        run = WritingRunInput(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
        self.run = run
        stored = ApprovedProfile(VERSION_ID, profile or a_profile()) if approved else None
        context = WriterContext(
            model=chat, limits=limits or Limits(), approved_profile=lambda: stored
        )
        self.events: list[RunEvent] = []
        self.asked: list[tuple[str, dict[str, Any]]] = []
        finals: list[dict[str, Any]] = []

        def finish(ref: GraphRunRef, final: Mapping[str, Any]) -> dict[str, Any]:
            finals.append(dict(final))
            return {}

        steps = AgentSteps(
            agent,
            initial_state=lambda ref: initial_state(run, self.brief),
            finish=finish,
            context=lambda ref, checkpoints: context,
            recursion_limit=recursion_limit(context.limits),
            record_progress=lambda ref, events: self.events.extend(events),
        )
        ref = GraphRunRef(WRITING_RUN_KIND, run.run_id)
        store = InMemorySaver(serde=JsonPlusSerializer(allowed_msgpack_modules=None))
        checkpoints = RunCheckpoints(store=store, thread_id=thread_id(ref.kind, ref.run_id))
        resume = None
        while True:
            step = steps.advance(ref, checkpoints, resume)
            if step.action is None:
                break
            self.asked.append((step.action.name, step.action.payload))
            if step.action.name == STORE_DRAFT:
                resume = _stored(step.action.action_id, store_error)
            else:
                resume = _outcome(step.action.action_id, decision)
        [self.final] = finals
        self.result = result_of(self.final)

    def kinds(self) -> list[str]:
        return [event.kind for event in self.events]


def _stored(action_id: str, error: str) -> ActionOutcome:
    if error:
        return ActionOutcome(action_id, False, {}, error)
    return ActionOutcome(action_id, True, {"content_item_id": ITEM_ID}, "")


def _outcome(action_id: str, decision: ActionOutcome | str) -> ActionOutcome:
    if isinstance(decision, ActionOutcome):
        return ActionOutcome(action_id, decision.succeeded, decision.result, decision.error)
    if decision == "approve":
        return ActionOutcome(action_id, True, {"status": "dry_run"}, "")
    return ActionOutcome(action_id, False, {}, decision)


# ------------------------------------------------------------------------------- the runs
def test_a_first_draft_that_passes_is_proposed_and_recorded_as_a_dry_run():
    chat = ScriptedChat(
        writer=[a_plan(), a_draft()], critic=[a_report(a_review(0), a_review(1), ranking=[1])]
    )

    run = Run(chat)

    ids = {
        "workspace_id": str(run.run.workspace_id),
        "project_id": str(run.run.project_id),
        "run_id": str(run.run.run_id),
    }
    [(stored, draft), (published, payload)] = run.asked
    assert (stored, published) == (STORE_DRAFT, PUBLISH_POST)
    assert payload == {**ids, "content_item_id": ITEM_ID, "text": "Offline? Keep writing."}
    assert {key: draft[key] for key in ids} == ids
    assert draft["text"] == "Offline? Keep writing."
    result = run.result
    assert result.content_item_id == ITEM_ID
    assert result.status is PostStatus.DRY_RUN
    assert result.text == "Offline? Keep writing."  # the critic ranked it first
    assert result.passed_critic and result.rounds == 1
    assert result.profile_version_id == str(VERSION_ID)
    assert [alternative["index"] for alternative in result.alternatives] == [0]
    assert result.claims[0]["evidence"] == [
        {"feature_id": "offline-sync", "file": "src/sync.ts", "lines": "10-40"}
    ]
    assert result.cost_usd == pytest.approx(0.03)


def test_the_writer_and_the_critic_are_sent_no_tools():
    chat = ScriptedChat(writer=[a_plan(), a_draft()], critic=[a_report(a_review(0), a_review(1))])

    Run(chat)

    assert [call["role"] for call in chat.calls] == ["writer", "writer", "critic"]
    assert all(call["tools"] is None for call in chat.calls)


def test_the_writer_is_shown_live_features_only_and_their_descriptions_as_data():
    chat = ScriptedChat(writer=[a_plan(), a_draft()], critic=[a_report(a_review(0), a_review(1))])

    Run(chat)

    shown = data_of(chat.sent_to("writer")[0])["writing_context"]["features"]
    assert shown == [
        {"id": "offline-sync", "description": "Notes sync when the device is back online."}
    ]


def test_a_variant_that_breaks_a_code_rule_is_not_sent_to_the_critic_nor_proposed():
    too_long = a_variant("word " * 70)
    chat = ScriptedChat(
        writer=[a_plan(), a_draft(too_long, a_variant())], critic=[a_report(a_review(1))]
    )

    run = Run(chat)

    critic_data = data_of(chat.sent_to("critic")[0])
    assert [variant["variant"] for variant in critic_data["variants"]] == [1]
    assert run.result.chosen["index"] == 1
    [alternative] = run.result.alternatives
    assert alternative["check"]["failures"][0]["rule"] == "length"
    assert alternative["review"] is None


def test_a_round_that_fails_is_revised_from_its_findings_and_the_rewrite_proposed():
    chat = ScriptedChat(
        writer=[a_plan(), a_draft(a_variant("Beats BetaCorp. Offline too.")), a_draft(a_variant())],
        critic=[a_report(a_review(0))],
    )

    run = Run(
        chat, brief=WritingBrief(platform=WritingPlatform.X, goal=WritingGoal.INTRODUCE, variants=1)
    )

    revise_messages = chat.sent_to("writer")[2]
    [finding] = data_of(revise_messages)["last_round"]
    assert any("BetaCorp" in found for found in finding["code_findings"])
    assert run.result.rounds == 2 and run.result.passed_critic
    assert run.final["earlier_rounds"][0]["round"] == 1


def test_the_critics_findings_and_suggestions_reach_the_rewrite():
    review = a_review(0, brand_voice="major")
    review["criteria"][0]["note"] = "too stiff"
    review["suggestions"] = ["say it warmly"]
    chat = ScriptedChat(
        writer=[a_plan(), a_draft(a_variant()), a_draft(a_variant("Offline? Keep writing."))],
        critic=[a_report(review), a_report(a_review(0))],
    )

    Run(
        chat, brief=WritingBrief(platform=WritingPlatform.X, goal=WritingGoal.INTRODUCE, variants=1)
    )

    [finding] = data_of(chat.sent_to("writer")[2])["last_round"]
    assert finding["critic_findings"] == ["brand_voice (major): too stiff"]
    assert finding["suggestions"] == ["say it warmly"]


def test_a_minor_failure_of_the_critic_is_advice_and_the_variant_passes():
    chat = ScriptedChat(
        writer=[a_plan(), a_draft(a_variant())],
        critic=[a_report(a_review(0, platform_fit="minor"))],
    )

    run = Run(
        chat, brief=WritingBrief(platform=WritingPlatform.X, goal=WritingGoal.INTRODUCE, variants=1)
    )

    assert run.result.passed_critic and run.result.rounds == 1


def test_at_the_round_limit_the_best_variant_without_a_risky_claim_is_proposed_as_not_passing():
    chat = ScriptedChat(
        writer=[a_plan(), a_draft(), a_draft()],
        critic=[
            a_report(a_review(0, facts="major"), a_review(1, facts="major")),
            a_report(a_review(0, facts="major", risky=True), a_review(1, brand_voice="major")),
        ],
    )

    run = Run(chat, limits=Limits(max_rounds=2))

    assert run.result.status is PostStatus.DRY_RUN
    assert not run.result.passed_critic
    assert run.result.chosen["index"] == 1
    assert "round limit" in run.result.reason


def test_at_the_round_limit_a_run_whose_every_variant_makes_a_risky_claim_fails():
    risky = a_report(a_review(0, risky=True), a_review(1, risky=True))
    chat = ScriptedChat(writer=[a_plan(), a_draft(), a_draft()], critic=[risky, risky])

    with pytest.raises(NoVariantToPropose, match="risky claim"):
        Run(chat, limits=Limits(max_rounds=2))


def test_the_same_failing_variants_resubmitted_end_the_run_at_the_round_limit():
    too_long = a_variant("word " * 70)
    chat = ScriptedChat(writer=[a_plan(), *[a_draft(too_long)] * 3], critic=[])

    with pytest.raises(NoVariantToPropose):
        Run(
            chat,
            brief=WritingBrief(platform=WritingPlatform.X, goal=WritingGoal.INTRODUCE, variants=1),
        )

    assert len(chat.sent_to("writer")) == 1 + 3  # the plan, the draft and two rewrites


def test_a_project_without_an_approved_profile_ends_the_run_before_any_model_call():
    chat = ScriptedChat(writer=[], critic=[])

    with pytest.raises(NoApprovedProfile):
        Run(chat, approved=False)

    assert chat.calls == []


@pytest.mark.parametrize(
    ("feature", "said"), [("ai-summary", "unreleased, not live"), ("dark-mode", "no feature")]
)
def test_a_brief_announcing_a_feature_that_is_not_live_ends_the_run(feature, said):
    chat = ScriptedChat(writer=[], critic=[])
    brief = WritingBrief(platform=WritingPlatform.X, goal=WritingGoal.ANNOUNCE, feature=feature)

    with pytest.raises(FeatureNotPublishable, match=said):
        Run(chat, brief=brief)

    assert chat.calls == []


def test_the_failures_of_a_run_are_what_the_worker_words():
    for error in (NoApprovedProfile, FeatureNotPublishable, NoVariantToPropose):
        assert issubclass(error, WRITING_FAILURES)


def test_an_announcement_plan_is_held_to_the_feature_and_its_changes_kept():
    chat = ScriptedChat(
        writer=[a_plan(feature_ids=["ai-summary"]), a_draft(a_variant())],
        critic=[a_report(a_review(0))],
    )
    brief = WritingBrief(
        platform=WritingPlatform.X, goal=WritingGoal.ANNOUNCE, feature="offline-sync", variants=1
    )

    run = Run(chat, brief=brief)

    assert run.result.plan["feature_ids"] == ["offline-sync"]
    assert run.result.plan_changes == [
        "dropped the feature 'ai-summary': not a live feature of the profile",
        "added the feature 'offline-sync': the brief announces it",
    ]


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (rules.REJECTED_OUTCOME, PostStatus.REJECTED),
        (rules.EXPIRED_OUTCOME, PostStatus.EXPIRED),
        ("the handler could not store the post", PostStatus.FAILED),
    ],
)
def test_a_post_that_was_not_published_is_recorded_as_it_ended(error, status):
    chat = ScriptedChat(writer=[a_plan(), a_draft()], critic=[a_report(a_review(0), a_review(1))])

    run = Run(chat, decision=error)

    assert run.result.status is status
    assert run.result.action_error == error


def test_a_claim_on_a_feature_that_is_not_live_fails_the_variant():
    claim = {"statement": "It summarizes notes.", "feature_ids": ["ai-summary"]}
    chat = ScriptedChat(
        writer=[a_plan(), a_draft(a_variant(claims=[claim]), a_variant())],
        critic=[a_report(a_review(1))],
    )

    run = Run(chat)

    [alternative] = run.result.alternatives
    assert alternative["check"]["failures"] == [
        {
            "rule": "claims",
            "finding": "claim 1 names 'ai-summary', which is not a live feature of the profile",
        }
    ]


def test_a_number_the_profile_does_not_hold_is_flagged_for_the_critic_and_the_person():
    chat = ScriptedChat(
        writer=[a_plan(), a_draft(a_variant("Try it 14 days. Syncs 3x faster offline."))],
        critic=[a_report(a_review(0))],
    )

    run = Run(
        chat, brief=WritingBrief(platform=WritingPlatform.X, goal=WritingGoal.INTRODUCE, variants=1)
    )

    [shown] = data_of(chat.sent_to("critic")[0])["variants"]
    assert shown["numbers_not_in_profile"] == ["3"]
    assert run.result.flagged_numbers == ["3"]
    [(_, draft), _] = run.asked
    assert draft["flagged_numbers"] == ["3"]


def test_a_language_the_profile_does_not_have_fails_the_variant():
    chat = ScriptedChat(
        writer=[a_plan(), a_draft(a_variant(language="de"), a_variant(language="en-GB"))],
        critic=[a_report(a_review(1))],
    )

    run = Run(chat)

    assert run.result.chosen["index"] == 1
    assert run.result.alternatives[0]["check"]["failures"][0]["rule"] == "language"


def test_a_profile_without_languages_checks_none():
    profile = a_profile(product={"name": "Acme Notes", "type": "b2b_saas"})
    chat = ScriptedChat(
        writer=[a_plan(), a_draft(a_variant(language="de"))], critic=[a_report(a_review(0))]
    )

    run = Run(
        chat,
        profile=profile,
        brief=WritingBrief(platform=WritingPlatform.X, goal=WritingGoal.INTRODUCE, variants=1),
    )

    assert run.result.language == "de" and run.result.check_report["passed"]


# ------------------------------------------------------------------------- weak models
def test_an_answer_in_a_markdown_fence_is_read():
    fenced = "```json\n" + json.dumps({"variants": [a_variant()]}) + "\n```"
    chat = ScriptedChat(writer=[a_plan(), text(fenced)], critic=[a_report(a_review(0))])

    run = Run(
        chat, brief=WritingBrief(platform=WritingPlatform.X, goal=WritingGoal.INTRODUCE, variants=1)
    )

    assert run.result.text == a_variant()["text"]


def test_variants_and_claims_a_weak_model_sends_as_json_text_are_read():
    variant = a_variant()
    variant["claims"] = json.dumps(variant["claims"])
    chat = ScriptedChat(
        writer=[a_plan(feature_ids='["offline-sync"]'), text({"variants": json.dumps([variant])})],
        critic=[a_report(a_review(0))],
    )

    run = Run(
        chat, brief=WritingBrief(platform=WritingPlatform.X, goal=WritingGoal.INTRODUCE, variants=1)
    )

    assert run.result.claims[0]["feature_ids"] == ["offline-sync"]


def test_an_answer_that_does_not_fit_is_told_so_and_asked_once_more():
    chat = ScriptedChat(
        writer=[text({"angle": "x", "format": "thread"}), a_plan(), a_draft(a_variant())],
        critic=[a_report(a_review(0))],
    )

    Run(
        chat, brief=WritingBrief(platform=WritingPlatform.X, goal=WritingGoal.INTRODUCE, variants=1)
    )

    told = chat.sent_to("writer")[1][-1]
    assert told["role"] == "user" and told["content"].startswith("Rejected")
    assert "feature_ids" in told["content"]


def test_two_unreadable_plans_end_the_run():
    chat = ScriptedChat(writer=[text("I would rather not."), text("{}")], critic=[])

    with pytest.raises(WritingError, match="plan could not be read after two attempts"):
        Run(chat)


def cut_off() -> ChatReply:
    """An answer a reasoning model left empty when it hit the role's length limit."""
    return ChatReply(
        message={"role": "assistant"}, model="vendor/m", cost_usd=0.01, finish_reason="length"
    )


def test_variants_cut_off_twice_end_the_run_saying_to_raise_the_limit():
    chat = ScriptedChat(writer=[a_plan(), cut_off(), cut_off()], critic=[])

    with pytest.raises(WritingError) as failed:
        Run(chat)

    assert "variants was cut off by the model's length limit, twice" in str(failed.value)
    assert rules.CUT_OFF_HINT in str(failed.value)


def test_a_cut_off_answer_then_an_unreadable_one_is_worded_as_unreadable():
    chat = ScriptedChat(writer=[a_plan(), cut_off(), text("{}")], critic=[])

    with pytest.raises(WritingError, match="variants could not be read after two attempts"):
        Run(chat)


def test_more_variants_than_the_brief_asked_for_are_cut_with_a_note():
    chat = ScriptedChat(
        writer=[a_plan(), a_draft(a_variant(), a_variant("Two."), a_variant("Three."))],
        critic=[a_report(a_review(0))],
    )

    run = Run(
        chat, brief=WritingBrief(platform=WritingPlatform.X, goal=WritingGoal.INTRODUCE, variants=1)
    )

    assert run.result.alternatives == []
    assert "round 1: kept the first 1 of 3 variants" in run.result.notes


def test_a_critic_report_that_leaves_out_a_variant_is_asked_again():
    chat = ScriptedChat(
        writer=[a_plan(), a_draft()],
        critic=[a_report(a_review(0)), a_report(a_review(0), a_review(1))],
    )

    Run(chat)

    told = chat.sent_to("critic")[1][-1]["content"]
    assert told.startswith("Rejected") and "no review for the variants [1]" in told


def test_an_unreadable_critic_report_proposes_nothing_of_the_round():
    chat = ScriptedChat(
        writer=[a_plan(), a_draft(a_variant())], critic=[text("fine"), text("all fine")]
    )

    with pytest.raises(NoVariantToPropose):
        Run(
            chat,
            brief=WritingBrief(platform=WritingPlatform.X, goal=WritingGoal.INTRODUCE, variants=1),
            limits=Limits(max_rounds=1),
        )


def test_a_run_whose_cost_is_used_up_asks_no_critic_and_fails_naming_the_limit():
    chat = ScriptedChat(writer=[a_plan(), text({"variants": [a_variant()]}, cost_usd=1.0)])

    with pytest.raises(NoVariantToPropose, match="cost limit"):
        Run(
            chat,
            brief=WritingBrief(platform=WritingPlatform.X, goal=WritingGoal.INTRODUCE, variants=1),
        )

    assert chat.sent_to("critic") == []


# ----------------------------------------------------------------------------- progress
def test_a_run_shows_its_progress_node_by_node():
    chat = ScriptedChat(writer=[a_plan(), a_draft()], critic=[a_report(a_review(0), a_review(1))])

    run = Run(chat)

    assert run.kinds() == [
        progress.WRITING_PREPARED,
        progress.POST_PLANNED,
        progress.VARIANTS_WRITTEN,
        progress.VARIANTS_CHECKED,
        progress.VARIANTS_CRITIQUED,
        progress.VARIANT_CHOSEN,
        progress.DRAFT_STORED,
        progress.POST_RECORDED,
    ]
    assert run.events[-1].detail == {"status": "dry_run"}


# ------------------------------------------------------------------------- the boundaries
def test_a_run_with_no_workflow_stops_where_it_asks_for_store_draft():
    chat = ScriptedChat(writer=[a_plan(), a_draft()], critic=[a_report(a_review(0), a_review(1))])
    run = WritingRunInput(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    stored = ApprovedProfile(VERSION_ID, a_profile())
    context = WriterContext(model=chat, limits=Limits(), approved_profile=lambda: stored)
    brief = WritingBrief(platform=WritingPlatform.X, goal=WritingGoal.INTRODUCE, variants=2)

    with pytest.raises(ActionAskedInLocalRun) as raised:
        run_from(
            agent,
            "prepare",
            initial_state(run, brief),
            recursion_limit=recursion_limit(Limits()),
            context=context,
        )

    assert raised.value.action_name == STORE_DRAFT


def test_the_declarations_load_no_graph_framework_or_database():
    code = (
        "import sys, json; import openmarketer_core.writing.writer_agent.definitions; "
        "print(json.dumps(sorted(sys.modules)))"
    )
    done = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True
    )
    loaded = set(json.loads(done.stdout))
    for package in ("langgraph", "sqlalchemy", "psycopg"):
        assert not any(name == package or name.startswith(f"{package}.") for name in loaded)


# ------------------------------------------------------------------------------ the draft
def test_the_draft_carries_what_the_content_item_stores_and_the_alternatives_reports():
    chat = ScriptedChat(
        writer=[a_plan(feature_ids=["ai-summary", "offline-sync"]), a_draft()],
        critic=[a_report(a_review(0), a_review(1, platform_fit="minor"), ranking=[0])],
    )

    run = Run(chat)

    [(_, draft), _] = run.asked
    assert set(draft) == {
        "workspace_id",
        "project_id",
        "run_id",
        "text",
        "plan",
        "plan_changes",
        "alternatives",
        "claims",
        "check_report",
        "critic_report",
        "flagged_numbers",
        "cost_usd",
    }
    assert draft["plan_changes"] == run.result.plan_changes != []
    [alternative] = draft["alternatives"]
    assert alternative["index"] == 1 and alternative["review"]["variant"] == 1
    assert draft["critic_report"]["variant"] == 0
    assert draft["claims"] == run.result.claims
    assert draft["cost_usd"] == pytest.approx(0.03)


def test_a_draft_that_could_not_be_stored_ends_the_run_before_any_proposal():
    chat = ScriptedChat(writer=[a_plan(), a_draft()], critic=[a_report(a_review(0), a_review(1))])

    with pytest.raises(WritingError, match="the draft could not be stored: the database is gone"):
        Run(chat, store_error="the database is gone")


def test_alternatives_that_would_make_the_draft_too_heavy_are_left_out_with_a_note():
    heavy = "界" * 2000  # breaks the length rule, and weighs 12 KiB as escaped JSON
    chat = ScriptedChat(
        writer=[a_plan(), a_draft(a_variant(), a_variant(heavy), a_variant(heavy))],
        critic=[a_report(a_review(0))],
    )

    run = Run(chat, brief=WritingBrief(platform=WritingPlatform.X, goal=WritingGoal.INTRODUCE))

    [(_, draft), _] = run.asked
    assert [alternative["index"] for alternative in draft["alternatives"]] == [1]
    assert run.result.alternatives == draft["alternatives"]
    assert "1 alternatives were left out of the draft for its size" in run.result.notes
    assert rules.draft_bytes(draft) <= rules.MAX_DRAFT_BYTES
