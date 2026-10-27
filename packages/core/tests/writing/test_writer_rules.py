"""Tests for the writer's rules: the writing context, the plan held, the code rules, the critic's
report, the decide rule and the prompts, which come from the same definitions as the checks."""

from typing import Any

import pytest

from openmarketer_core.profile import ProductProfile
from openmarketer_core.writing.brief import WritingBrief, WritingGoal, WritingPlatform
from openmarketer_core.writing.writer_agent import rules
from openmarketer_core.writing.writer_agent.rules import (
    CRITERIA,
    Criterion,
    CriticReport,
    Draft,
    Limits,
    Plan,
    PostFormat,
    PostStatus,
    Variant,
)

PROFILE = ProductProfile.model_validate(
    {
        "product": {"name": "Acme Notes", "type": "b2b_saas", "languages": ["en", "tr"]},
        "features": [
            {"id": "offline-sync", "description": "Works offline for 30 days.", "status": "live"},
            {"id": "ai-summary", "description": "Summaries.", "status": "unreleased"},
            {"id": "tags", "description": "Tag notes.", "status": "live"},
        ],
        "brand": {"do_not_mention": ["BetaCorp", "free forever"]},
    }
)
CONTEXT = rules.writing_context(PROFILE)
X = rules.situation(WritingPlatform.X, CONTEXT)
INTRODUCE = WritingBrief(platform=WritingPlatform.X, goal=WritingGoal.INTRODUCE)


def variant(text: str = "Write anywhere.", **changes: Any) -> Variant:
    data = {"text": text, "angle": "a", "language": "en", "claims": [], **changes}
    return Variant.model_validate(data)


def failures(v: Variant) -> list[str]:
    report = rules.check_variant(v, X, rules.profile_numbers(CONTEXT))
    return [failure["rule"] for failure in report["failures"]]


# ---------------------------------------------------------------------- writing context
def test_the_writing_context_holds_live_features_only():
    assert rules.live_feature_ids(CONTEXT) == ["offline-sync", "tags"]


# ------------------------------------------------------------------------------- the plan
def test_a_plan_keeps_live_features_once_and_records_what_it_dropped():
    plan = Plan(angle="a", feature_ids=["tags", "tags", "ai-summary"], format=PostFormat.TIP)

    held = rules.held_plan(plan, INTRODUCE, CONTEXT)

    assert held.plan == {"angle": "a", "feature_ids": ["tags"], "format": "tip"}
    assert held.changes == ["dropped the feature 'ai-summary': not a live feature of the profile"]


def test_an_announcement_rests_on_its_feature_first():
    brief = WritingBrief(
        platform=WritingPlatform.X, goal=WritingGoal.ANNOUNCE, feature="offline-sync"
    )
    plan = Plan(angle="a", feature_ids=["tags", "offline-sync"], format=PostFormat.STATEMENT)

    held = rules.held_plan(plan, brief, CONTEXT)

    assert held.plan["feature_ids"] == ["offline-sync", "tags"]
    assert held.changes == []


# --------------------------------------------------------------------------- code rules
def test_a_variant_that_keeps_every_rule_passes():
    assert failures(variant()) == []


def test_a_text_over_xs_weighted_length_fails():
    assert failures(variant("界" * 141)) == ["length"]
    assert failures(variant("界" * 140)) == []


def test_a_do_not_mention_term_is_matched_without_regard_to_case():
    assert failures(variant("Not like BETACORP.")) == ["do_not_mention"]
    assert failures(variant("It is FREE  forever.")) == ["do_not_mention"]


def test_a_claim_without_a_feature_fails():
    claim = {"statement": "It is fast.", "feature_ids": []}
    assert failures(variant(claims=[claim])) == ["claims"]


def test_links_mentions_and_a_third_hashtag_fail():
    assert failures(variant("See acme.app")) == ["no_links"]
    assert failures(variant("Thanks @acme")) == ["no_mentions"]
    assert failures(variant("#a #b #c")) == ["hashtags"]
    assert failures(variant("#a #b")) == []


def test_a_language_is_one_of_the_profiles_or_a_region_of_one():
    assert failures(variant(language="tr")) == []
    assert failures(variant(language="en-US")) == []
    assert failures(variant(language="de")) == ["language"]


def test_numbers_the_profile_holds_are_not_flagged_and_others_are():
    report = rules.check_variant(variant("30 days offline, 2,000 notes."), X, {"30"})
    assert report["flagged_numbers"] == ["2000"]
    assert report["passed"]


def test_the_numbers_of_the_profile_are_read_from_its_words():
    assert rules.profile_numbers(CONTEXT) == {"30"}


# ------------------------------------------------------------------------------ the critic
def review(index: int, *, risky: bool = False, **failing: str) -> dict[str, Any]:
    return {
        "variant": index,
        "criteria": [
            {
                "criterion": c.value,
                "passed": c.value not in failing,
                "severity": failing.get(c.value, "none"),
                "note": "",
            }
            for c in Criterion
        ],
        "risky_claim": risky,
        "risky_note": "",
        "suggestions": [],
    }


def test_a_report_must_review_every_variant_sent_on_every_criterion_once():
    partial = review(0)
    partial["criteria"] = partial["criteria"][:2]
    report = CriticReport.model_validate({"reviews": [partial, review(5)], "ranking": []})

    problems = rules.critic_problems(report, [0, 1])

    assert "reviews name variants you were not given: [5]" in problems
    assert "no review for the variants [1]" in problems
    assert any("each criterion once" in problem for problem in problems)


def test_a_long_note_of_the_critic_is_cut_not_refused():
    long = review(0)
    long["criteria"][0]["note"] = "x" * 1000
    report = CriticReport.model_validate({"reviews": [long], "ranking": [0]})
    assert len(report.reviews[0].criteria[0].note) == rules.MAX_NOTE_CHARS


def test_a_major_failure_or_a_risky_claim_fails_the_variant_and_a_minor_one_does_not():
    assert rules.critic_passes(review(0, facts="minor"))
    assert not rules.critic_passes(review(0, facts="major"))
    assert not rules.critic_passes(review(0, facts="none"))  # failed without a severity
    assert not rules.critic_passes(review(0, risky=True))


# ---------------------------------------------------------------------------- decide rule
PASSED = {"passed": True, "failures": [], "flagged_numbers": []}
BROKEN = {"passed": False, "failures": [{"rule": "length", "finding": ""}]}


def decide(checks, reviews, ranking=(), round_no=1, cost_usd=0.0):
    critique = {"reviews": reviews, "ranking": list(ranking)} if reviews else {}
    return rules.decide(
        checks=checks, critique=critique, round_no=round_no, cost_usd=cost_usd, limits=Limits()
    )


def test_the_passing_variant_the_critic_ranks_first_is_proposed():
    decision = decide([PASSED, PASSED], [review(0), review(1)], ranking=[1, 0])
    assert (decision.next, decision.chosen, decision.passed_critic) == ("propose", 1, True)


def test_a_ranking_that_names_a_failing_variant_is_not_followed():
    decision = decide([BROKEN, PASSED], [review(1)], ranking=[0])
    assert decision.chosen == 1


def test_no_pass_with_rounds_and_cost_left_revises():
    assert decide([PASSED], [review(0, facts="major")]).next == "revise"


def test_no_pass_at_the_cost_limit_falls_back_as_at_the_round_limit():
    decision = decide([PASSED], [review(0, facts="major")], cost_usd=0.5)
    assert (decision.next, decision.chosen, decision.passed_critic) == ("propose", 0, False)
    assert "cost limit" in decision.reason


def test_at_the_limit_the_fallback_prefers_the_variant_with_fewer_failed_criteria():
    reviews = [review(0, facts="major", brand_voice="major"), review(1, facts="major")]
    assert decide([PASSED, PASSED], reviews, round_no=3).chosen == 1


def test_at_the_limit_a_variant_the_critic_did_not_review_is_never_proposed():
    assert decide([PASSED], [], round_no=3).next == "fail"


def test_at_the_limit_a_variant_that_broke_a_code_rule_is_never_proposed():
    assert decide([BROKEN], [review(0)], round_no=3).next == "fail"


# --------------------------------------------------------------------------- the outcome
def test_the_outcome_of_publish_post_says_how_the_post_ended():
    assert rules.post_status(True, "") is PostStatus.DRY_RUN
    assert rules.post_status(False, rules.REJECTED_OUTCOME) is PostStatus.REJECTED
    assert rules.post_status(False, rules.EXPIRED_OUTCOME) is PostStatus.EXPIRED
    assert rules.post_status(False, "the handler failed") is PostStatus.FAILED


# ----------------------------------------------------------------- prompts and schemas
def test_every_field_a_model_fills_is_described_in_its_prompt():
    assert set(rules.PLAN_FIELDS) == set(Plan.model_fields)
    assert set(rules.VARIANT_FIELDS) == set(Variant.model_fields)
    assert set(rules.CLAIM_FIELDS) == set(rules.Claim.model_fields)
    assert set(rules.REPORT_FIELDS) == set(CriticReport.model_fields)
    assert set(rules.REVIEW_FIELDS) == set(rules.VariantReview.model_fields)
    assert set(rules.CRITERION_FIELDS) == set(rules.CriterionVerdict.model_fields)
    assert set(Draft.model_fields) == {"variants"}


def test_the_draft_prompt_tells_every_code_rule_with_the_platforms_values():
    prompt = rules.draft_system_prompt(X, 3)
    for rule in rules.CODE_RULES:
        told = rule.told(X)
        assert told is not None and told in prompt
    assert "at most 280 as X counts it" in prompt
    assert "At most 2 hashtags." in prompt
    assert "(en, tr)" in prompt


def test_a_rule_that_does_not_apply_is_neither_told_nor_checked():
    bare = rules.situation(
        WritingPlatform.X, {**CONTEXT, "do_not_mention": [], "product": {"languages": []}}
    )
    prompt = rules.draft_system_prompt(bare, 1)
    assert "do_not_mention" not in prompt and "product's languages" not in prompt
    report = rules.check_variant(variant("BetaCorp", language="de"), bare, set())
    assert report["passed"]


def test_the_critic_prompt_names_every_criterion():
    prompt = rules.critic_system_prompt(X)
    for criterion, meaning in CRITERIA.items():
        assert f"- {criterion.value}: {meaning}" in prompt


@pytest.mark.parametrize("schema", [Plan, Draft, Variant, rules.Claim, CriticReport])
def test_the_submission_schemas_forbid_extra_fields(schema):
    assert schema.model_json_schema()["additionalProperties"] is False
