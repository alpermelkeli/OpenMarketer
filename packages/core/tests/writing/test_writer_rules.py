"""Tests for the writer's rules: the writing context, the code rules, the decide rule, the plan
the content item stores and the prompts, which come from the same definitions as the checks."""

from typing import Any

import pytest

from openmarketer_core.profile import ProductProfile
from openmarketer_core.writing.brief import WritingPlatform
from openmarketer_core.writing.writer_agent import rules
from openmarketer_core.writing.writer_agent.rules import (
    Draft,
    Limits,
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
IMAGE = {
    "asset_id": "a",
    "version_id": "v",
    "platform": "x_post",
    "goal": "g",
    "words_on_image": ["Write anywhere", "Even offline"],
}
RESEARCH = {"run_id": "r", "questions": ["q"], "findings": [{"statement": "s", "sites": []}]}
X_WITH_INPUTS = rules.situation(
    WritingPlatform.X, {**CONTEXT, "image": IMAGE, "research": RESEARCH}
)


def variant(text: str = "Write anywhere.", **changes: Any) -> Variant:
    data = {"text": text, "angle": "a", "language": "en", "claims": [], **changes}
    return Variant.model_validate(data)


INSTAGRAM = rules.situation(WritingPlatform.INSTAGRAM, {**CONTEXT, "image": IMAGE})


def failures(v: Variant, s: rules.Situation = X) -> list[str]:
    report = rules.check_variant(v, s, rules.profile_numbers(CONTEXT))
    return [failure["rule"] for failure in report["failures"]]


# ---------------------------------------------------------------------- writing context
def test_the_writing_context_holds_live_features_only():
    assert rules.live_feature_ids(CONTEXT) == ["offline-sync", "tags"]


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


def test_an_instagram_caption_over_2200_characters_fails():
    assert failures(variant("a" * 2201), INSTAGRAM) == ["length"]
    assert failures(variant("a" * 2200), INSTAGRAM) == []


def test_an_instagram_caption_may_run_over_several_lines():
    caption = "Write anywhere.\n\nEven on a plane.\n\n#notes #offline #productivity"
    assert failures(variant(caption), INSTAGRAM) == []


def test_an_address_in_an_instagram_caption_fails_as_a_link():
    assert failures(variant("Get it at https://acme.app"), INSTAGRAM) == ["no_links"]
    assert failures(variant("Get it at acme.app"), INSTAGRAM) == ["no_links"]
    assert failures(variant("Get it: link in bio."), INSTAGRAM) == []


def test_an_instagram_caption_may_carry_30_hashtags_and_not_31():
    assert failures(variant(" ".join(f"#tag{n}" for n in range(30))), INSTAGRAM) == []
    assert failures(variant(" ".join(f"#tag{n}" for n in range(31))), INSTAGRAM) == ["hashtags"]


def test_the_writer_is_told_instagrams_habits():
    prompt = rules.write_system_prompt(INSTAGRAM, 3)

    assert "at most 2200 as Instagram counts it" in prompt
    assert "At most 30 hashtags." in prompt
    for habit in INSTAGRAM.platform.habits:
        assert habit in prompt
    assert '"link in bio"' in prompt


def test_an_x_post_is_told_no_habits_of_another_platform():
    assert "link in bio" not in rules.write_system_prompt(X, 3)


def test_a_bare_domain_under_any_top_level_domain_fails_as_a_link():
    assert failures(variant("Try evil.top, it is fast.")) == ["no_links"]
    assert failures(variant("Try evil\u3002com, it is fast.")) == ["no_links"]


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


# ---------------------------------------------------------------------------- decide rule
def kept(*flagged: str) -> dict[str, Any]:
    return {"passed": True, "failures": [], "flagged_numbers": list(flagged)}


BROKEN = {"passed": False, "failures": [{"rule": "length", "finding": ""}], "flagged_numbers": []}


def decide(checks, round_no=1, cost_usd=0.0):
    return rules.decide(checks=checks, round_no=round_no, cost_usd=cost_usd, limits=Limits())


def test_the_first_variant_that_keeps_every_code_rule_is_proposed():
    decision = decide([BROKEN, kept(), kept()])
    assert (decision.next, decision.chosen) == ("propose", 1)


def test_the_variant_with_fewer_flagged_numbers_is_preferred():
    assert decide([kept("3", "40"), kept("3"), kept("7")]).chosen == 1


def test_a_variant_that_keeps_every_code_rule_is_proposed_at_the_limit_too():
    decision = decide([kept()], round_no=3, cost_usd=0.5)
    assert (decision.next, decision.chosen) == ("propose", 0)


def test_no_variant_that_keeps_the_code_rules_with_rounds_and_cost_left_revises():
    assert decide([BROKEN, BROKEN]).next == "revise"


@pytest.mark.parametrize(("round_no", "cost_usd", "limit"), [(3, 0.0, "round"), (1, 0.5, "cost")])
def test_no_variant_that_keeps_the_code_rules_at_a_limit_fails_naming_it(round_no, cost_usd, limit):
    decision = decide([BROKEN], round_no=round_no, cost_usd=cost_usd)
    assert (decision.next, decision.chosen) == ("fail", -1)
    assert f"the {limit} limit was hit" in decision.reason


# ------------------------------------------------------------------------------ the draft
def test_the_stored_plan_is_the_chosen_variants_angle_and_the_features_of_its_claims():
    claims = [
        {"statement": "a", "feature_ids": ["tags", "offline-sync"]},
        {"statement": "b", "feature_ids": ["offline-sync", "search", "export"]},
    ]

    plan = rules.plan_of({"angle": "A question.", "claims": claims})

    assert plan == {"angle": "A question.", "feature_ids": ["tags", "offline-sync", "search"]}


def test_the_draft_keeps_the_shape_the_content_item_stores_with_no_review():
    variants = [variant().model_dump(), variant("Offline? Keep writing.").model_dump()]

    draft = rules.draft_of(
        chosen=1, variants=variants, checks=[kept(), kept()], context=CONTEXT, cost_usd=0.01
    )

    assert (draft["plan_changes"], draft["critic_report"]) == ([], {})
    [alternative] = draft["alternatives"]
    assert (alternative["index"], alternative["review"]) == (0, None)


# --------------------------------------------------------------------------- the outcome
def test_the_outcome_of_publish_post_says_how_the_post_ended():
    assert rules.post_status(True, "") is PostStatus.DRY_RUN
    assert rules.post_status(False, rules.REJECTED_OUTCOME) is PostStatus.REJECTED
    assert rules.post_status(False, rules.EXPIRED_OUTCOME) is PostStatus.EXPIRED
    assert rules.post_status(False, "the handler failed") is PostStatus.FAILED


# ----------------------------------------------------------------- prompts and schemas
def test_every_field_the_writer_fills_is_described_in_its_prompt():
    assert set(rules.VARIANT_FIELDS) == set(Variant.model_fields)
    assert set(rules.CLAIM_FIELDS) == set(rules.Claim.model_fields)
    assert set(Draft.model_fields) == {"variants"}


def test_the_writer_is_asked_for_variants_that_are_different_takes():
    prompt = rules.write_system_prompt(X, 3)
    assert "Write 3 variants" in prompt
    assert "Variants are different ideas, not rewordings." in prompt


def test_the_prompt_tells_every_code_rule_with_the_platforms_values():
    prompt = rules.write_system_prompt(X_WITH_INPUTS, 3)
    for rule in rules.CODE_RULES:
        told = rule.told(X_WITH_INPUTS)
        assert told is not None and told in prompt
    assert "at most 280 as X counts it" in prompt
    assert "At most 2 hashtags." in prompt
    assert "(en, tr)" in prompt


def test_a_rule_that_does_not_apply_is_neither_told_nor_checked():
    bare = rules.situation(
        WritingPlatform.X, {**CONTEXT, "do_not_mention": [], "product": {"languages": []}}
    )
    prompt = rules.write_system_prompt(bare, 1)
    assert "do_not_mention" not in prompt and "product's languages" not in prompt
    report = rules.check_variant(variant("BetaCorp", language="de"), bare, set())
    assert report["passed"]


@pytest.mark.parametrize("schema", [Draft, Variant, rules.Claim])
def test_the_submission_schemas_forbid_extra_fields(schema):
    assert schema.model_json_schema()["additionalProperties"] is False


# ------------------------------------------------------------------ the image and the research
@pytest.mark.parametrize(
    "text",
    [
        "Write anywhere. Even offline.",
        "write ANYWHERE — even offline!",
        "Write anywhere\nEven offline",
    ],
)
def test_a_text_that_is_only_the_words_on_the_image_breaks_a_rule(text):
    report = rules.check_variant(variant(text), X_WITH_INPUTS, set())

    assert [failure["rule"] for failure in report["failures"]] == ["image_words"]


def test_a_text_that_adds_to_the_words_on_the_image_keeps_the_rule():
    report = rules.check_variant(
        variant("Write anywhere, even offline: on the plane too."), X_WITH_INPUTS, set()
    )

    assert report["passed"]


def test_a_post_without_an_image_is_neither_told_nor_checked_the_image_rule():
    [image_words] = [rule for rule in rules.CODE_RULES if rule.key == "image_words"]
    assert image_words.told(X) is None
    assert failures(variant("Write anywhere. Even offline.")) == []


def test_the_prompt_of_a_run_given_inputs_says_how_to_use_them():
    prompt = rules.write_system_prompt(X_WITH_INPUTS, 1)
    assert "\nThe image\n" in prompt and "\nThe research\n" in prompt
    assert "(image)" in prompt and "(research)" in prompt


def test_the_prompt_of_a_run_without_inputs_names_none():
    prompt = rules.write_system_prompt(X, 1)
    assert "The image" not in prompt and "The research" not in prompt
    assert "(image)" not in prompt and "(research)" not in prompt


def test_the_writer_is_told_a_finding_is_never_a_claim():
    assert "It is never a claim about the product" in rules.write_system_prompt(X_WITH_INPUTS, 1)


def test_claims_take_the_evidence_of_their_features_in_the_writing_context():
    context = {
        "features": [
            {"id": "offline-sync", "evidence": [{"file": "src/sync.ts", "lines": "10-40"}]},
            {"id": "tags", "evidence": [{"file": "x" * (rules.MAX_EVIDENCE_FILE_CHARS + 1)}]},
        ]
    }
    claims = [{"statement": "Syncs offline.", "feature_ids": ["offline-sync", "tags", "gone"]}]

    assert rules.claims_with_evidence(claims, context) == [
        {
            "statement": "Syncs offline.",
            "feature_ids": ["offline-sync", "tags", "gone"],
            "evidence": [{"feature_id": "offline-sync", "file": "src/sync.ts", "lines": "10-40"}],
        }
    ]
