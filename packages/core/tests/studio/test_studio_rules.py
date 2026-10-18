"""Tests for the creative studio's rules: what follows a round, what an operation may name."""

import json
import subprocess
import sys

from openmarketer_core.agent_runtime.model_input import read_strict_json
from openmarketer_core.studio.brand_kit import EMPTY_BRAND_KIT, BrandKit
from openmarketer_core.studio.image_checks import COPY, WRONG_SIZE
from openmarketer_core.studio.studio_agent import rules
from openmarketer_core.studio.studio_agent.rules import (
    Limits,
    Operation,
    RunImages,
    Technique,
    VerifierReport,
    decide,
)

V1 = "11111111-1111-4111-8111-111111111111"
V2 = "22222222-2222-4222-8222-222222222222"


def decided(failures, technique=Technique.GENERATED, earlier=(), round_no=1, cost=0.0):
    return decide(
        passed=not failures,
        failures=failures,
        technique=technique,
        earlier=list(earlier),
        round_no=round_no,
        cost_usd=cost,
        limits=Limits(),
    )


def failed(technique: Technique, *failures: str) -> dict:
    return {"round": 1, "technique": technique.value, "failures": list(failures)}


# ------------------------------------------------------------------------------- decide
def test_a_pass_is_finalized():
    assert decided([]).next == "finalize"


def test_a_failure_at_the_round_limit_escalates():
    assert decided([COPY], round_no=4).next == "escalate"


def test_a_failure_at_the_cost_limit_escalates():
    assert decided([COPY], cost=1.0).next == "escalate"


def test_a_first_failure_is_edited_with_the_same_technique():
    decision = decided([COPY])
    assert (decision.next, decision.technique) == ("edit", Technique.GENERATED)


def test_wrong_words_twice_in_a_generated_image_switch_to_background_overlay():
    decision = decided([COPY], earlier=[failed(Technique.GENERATED, COPY)], round_no=2)
    assert (decision.next, decision.technique) == ("edit", Technique.BACKGROUND_OVERLAY)


def test_another_failure_twice_in_a_generated_image_switches_to_an_html_reference():
    decision = decided(
        ["verifier:composition"],
        earlier=[failed(Technique.GENERATED, "verifier:composition")],
        round_no=2,
    )
    assert decision.technique == Technique.HTML_REFERENCE


def test_an_overlay_that_fails_twice_falls_back_to_composing_in_code():
    decision = decided(
        [COPY], Technique.BACKGROUND_OVERLAY, [failed(Technique.BACKGROUND_OVERLAY, COPY)], 2
    )
    assert decision.technique == Technique.COMPOSED


def test_a_composition_that_fails_twice_escalates():
    decision = decided([COPY], Technique.COMPOSED, [failed(Technique.COMPOSED, COPY)], 2)
    assert decision.next == "escalate"


def test_a_failure_another_technique_had_before_is_edited_not_switched():
    decision = decided([COPY], Technique.BACKGROUND_OVERLAY, [failed(Technique.GENERATED, COPY)], 2)
    assert (decision.next, decision.technique) == ("edit", Technique.BACKGROUND_OVERLAY)


def test_two_different_failures_in_a_row_are_edited():
    decision = decided([WRONG_SIZE], earlier=[failed(Technique.GENERATED, COPY)], round_no=2)
    assert (decision.next, decision.technique) == ("edit", Technique.GENERATED)


# --------------------------------------------------------------------------- operations
def problems(operation: dict, technique=Technique.GENERATED, cost=0.0, this_round=()) -> list:
    return rules.operation_problems(
        Operation.model_validate(operation),
        technique=technique,
        images=RunImages(
            run_versions=frozenset({V1, V2}),
            this_round=frozenset(this_round),
            reference_assets=frozenset(),
        ),
        cost_usd=cost,
        limits=Limits(),
    )


def test_an_operation_of_another_technique_is_refused():
    [problem] = problems({"op": "run_python", "code": "pass"})
    assert "not an operation of the technique generated" in problem


def test_an_html_reference_generation_must_name_its_rendered_layout():
    [problem] = problems({"op": "generate_image", "prompt": "a cat"}, Technique.HTML_REFERENCE)
    assert "reference_version_ids" in problem


def test_an_image_that_is_not_the_runs_cannot_be_named():
    other = "33333333-3333-4333-8333-333333333333"
    [problem] = problems({"op": "edit_image", "source_version_id": other, "instruction": "x"})
    assert "not an image of this run" in problem


def test_a_candidate_must_be_an_image_of_this_round():
    [problem] = problems({"op": "candidate_ready", "version_id": V1}, this_round={V2})
    assert "made in this round" in problem


def test_a_generation_the_cost_limit_cannot_pay_for_is_refused():
    [problem] = problems({"op": "generate_image", "prompt": "a cat"}, cost=0.95)
    assert "cost limit" in problem


def test_an_operation_without_its_fields_is_told_which():
    assert problems({"op": "edit_image"}) == [
        "edit_image needs source_version_id",
        "edit_image needs instruction",
    ]


def test_lists_a_weak_model_sends_as_json_text_are_read_as_lists():
    sent = {"op": "generate_image", "prompt": "a cat", "reference_version_ids": f'["{V1}"]'}
    operation = read_strict_json(Operation, rules.unwrapped_arguments(Operation, sent))
    assert operation.reference_version_ids == [V1]


def test_text_that_reads_as_json_stays_text_in_a_text_field():
    sent = {"op": "render_html", "html": '["not a list"]'}
    assert rules.unwrapped_arguments(Operation, sent)["html"] == '["not a list"]'


# ------------------------------------------------------------------------------- prompts
def test_the_generation_prompt_carries_every_line_of_copy_verbatim():
    prompt = rules.generation_prompt(
        "A desk at dawn.", ["Launch day", "Try it free"], Technique.GENERATED
    )
    assert prompt.startswith("A desk at dawn.")
    assert '"Launch day"\n"Try it free"' in prompt


def test_a_background_for_an_overlay_is_asked_without_any_text():
    prompt = rules.generation_prompt("A desk.", ["Launch day"], Technique.BACKGROUND_OVERLAY)
    assert "Launch day" not in prompt
    assert "no text" in prompt


LIVE_PROMPT = (
    "Square Instagram launch background, dark minimal aesthetic. Deep navy-teal gradient with "
    "plenty of clean empty space. Flat modern product-launch mood, no text, no letters, no "
    "numbers, no logos, no UI labels, no watermark."
)


def test_a_prompt_with_copy_loses_the_sentences_that_forbid_text():
    prompt = rules.generation_prompt(LIVE_PROMPT, ["Sorukasam artık yayında"], Technique.GENERATED)
    assert "no text" not in prompt
    assert "no letters" not in prompt
    assert "Flat modern product-launch mood, no logos, no UI labels, no watermark." in prompt
    assert '"Sorukasam artık yayında"' in prompt


def test_an_edit_with_copy_loses_the_sentences_that_forbid_text():
    instruction = rules.edit_instruction(
        "Brighten the sky. Do not include any text or letters.", ["Launch"], Technique.GENERATED
    )
    assert instruction.startswith("Brighten the sky.\n\n")
    assert "Do not include" not in instruction


def test_a_background_for_an_overlay_keeps_its_bans_of_text():
    prompt = rules.generation_prompt(LIVE_PROMPT, ["Launch"], Technique.BACKGROUND_OVERLAY)
    assert prompt.startswith(LIVE_PROMPT)


def test_a_prompt_without_copy_keeps_its_bans_of_text():
    assert rules.generation_prompt(LIVE_PROMPT, [], Technique.GENERATED).startswith(LIVE_PROMPT)


def test_the_text_bans_taken_out_are_only_bans_of_text():
    assert rules.without_text_bans("A clean desk with no text, no people.") == (
        "A clean desk, no people."
    )
    assert rules.without_text_bans("A text-free poster, without any lettering at all.") == (
        "A poster."
    )
    assert rules.without_text_bans("No characters, no text-heavy layout.") == (
        "No characters, no text-heavy layout."
    )


def test_the_brief_s_placement_goes_with_the_copy():
    prompt = rules.generation_prompt(
        "A desk.", ["Launch"], Technique.GENERATED, copy_placement="Centered, upper third."
    )
    assert prompt.endswith("Where the words go: Centered, upper third.")


def test_the_placement_is_bounded():
    prompt = rules.generation_prompt(
        "A desk.", ["Launch"], Technique.HTML_REFERENCE, copy_placement="x" * 2000
    )
    assert prompt.endswith("Where the words go: " + "x" * rules.MAX_PLACEMENT_CHARS)


def test_a_background_for_an_overlay_leaves_space_where_the_words_go():
    prompt = rules.generation_prompt(
        "A desk.", ["Launch"], Technique.BACKGROUND_OVERLAY, copy_placement="Lower third."
    )
    assert prompt.endswith("Leave calm space where the words will go: Lower third.")


A_KIT = BrandKit(
    name="Sorukasam",
    kind="mobile app on ios, android",
    audience="high-school students preparing for exams",
    features=("Solves questions from a photo.", "Tracks mistakes.", "Daily quizzes.", "Ranks."),
    do_not_mention=("RivalApp",),
)


def test_the_product_block_tells_what_the_product_is_as_context():
    about = rules.about_product(A_KIT, ["Sorukasam artık yayında"])
    assert about.startswith("About the product")
    assert "- Product: Sorukasam" in about
    assert "- What it is: mobile app on ios, android" in about
    assert "- For: high-school students preparing for exams" in about
    assert "only the exact text below is written" in about


def test_the_product_block_shows_at_most_three_features():
    about = rules.about_product(A_KIT, [])
    assert about.count("- Feature:") == rules.MAX_ABOUT_FEATURES
    assert "Ranks." not in about
    assert "do not write these words in the image" in about


def test_the_product_block_is_bounded():
    long = "x" * 1000
    kit = BrandKit(name=long, kind=long, audience=long, features=(long,) * 3)
    assert len(rules.about_product(kit, [])) <= rules.MAX_ABOUT_CHARS


def test_the_product_block_leaves_out_every_term_not_to_mention():
    kit = BrandKit(
        name="Acme",
        audience="people leaving RivalApp",
        features=("Imports from rivalapp.", "Syncs notes."),
        do_not_mention=("RivalApp",),
    )
    about = rules.about_product(kit, [])
    assert "rivalapp" not in about.casefold()
    assert "- Feature: Syncs notes." in about


def test_an_empty_kit_adds_nothing_to_the_prompt():
    assert rules.about_product(EMPTY_BRAND_KIT, ["Launch"]) == ""
    prompt = rules.generation_prompt("A desk.", ["Launch"], Technique.GENERATED, about="")
    assert prompt.startswith("A desk.\n\nThe image shows exactly this text")


def test_the_product_block_goes_into_every_generation_and_edit():
    about = rules.about_product(A_KIT, ["Launch"])
    for technique in Technique:
        assert about in rules.generation_prompt("A desk.", ["Launch"], technique, about=about)
        assert about in rules.edit_instruction("Brighter.", [], technique, about=about)


def test_the_composed_prompt_fits_its_payload():
    prompt = rules.generation_prompt(
        "p" * rules.MAX_PROMPT_CHARS,
        ["c" * 120] * 6,
        Technique.GENERATED,
        copy_placement="x" * rules.MAX_PLACEMENT_CHARS,
        about=rules.about_product(
            BrandKit(name="n" * 300, kind="k" * 300, audience="a" * 300, features=("f",) * 5),
            ["c"],
        ),
    )
    assert len(prompt) <= rules.MAX_COMPOSED_PROMPT_CHARS


def test_the_brief_asks_for_a_picture_that_does_not_forbid_the_words():
    assert 'never says "no text"' in rules.BRIEF_SYSTEM_PROMPT


def test_the_prompts_say_the_copy_is_never_written_or_changed():
    for prompt in (rules.BRIEF_SYSTEM_PROMPT, rules.STUDIO_SYSTEM_PROMPT):
        assert "Never write, change" in prompt


# ------------------------------------------------------------------------------- reviews
def a_report(**failing: str) -> dict:
    return {
        "criteria": [
            {
                "criterion": name,
                "passed": name not in failing,
                "severity": failing.get(name, "none"),
                "note": "",
            }
            for name in rules.CRITERIA
        ],
        "edit_suggestions": [],
    }


def test_a_verifier_report_must_judge_every_criterion():
    report = a_report()
    report["criteria"] = report["criteria"][1:]
    assert rules.verifier_problems(VerifierReport.model_validate(report)) == [
        "judge the criterion legibility"
    ]


def test_only_a_major_or_critical_verdict_fails_a_candidate():
    report = a_report(legibility="minor", composition="major")
    assert rules.verifier_failures(report) == ["verifier:composition"]


def test_the_best_candidate_has_the_fewest_failures_and_then_is_the_latest():
    candidates = [
        {"version_id": V1, "failures": [COPY]},
        {"version_id": V2, "failures": [COPY]},
        {"version_id": "x", "failures": [COPY, WRONG_SIZE]},
    ]
    best = rules.best_candidate(candidates)
    assert best is not None and best["version_id"] == V2


def test_the_submission_schemas_forbid_extra_fields():
    for schema in (Operation, rules.Brief, rules.EditPlan, VerifierReport):
        assert schema.model_json_schema()["additionalProperties"] is False


def test_the_declarations_load_no_graph_framework_database_or_pillow():
    code = (
        "import sys, json; import openmarketer_core.studio.studio_agent.definitions; "
        "print(json.dumps(sorted(sys.modules)))"
    )
    done = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True
    )
    loaded = set(json.loads(done.stdout))
    for package in ("langgraph", "sqlalchemy", "psycopg", "PIL"):
        assert not any(name == package or name.startswith(f"{package}.") for name in loaded)


# ----------------------------------------------------------------------- first technique
def test_a_run_never_starts_with_the_background_overlay_fallback():
    assert (
        rules.first_technique(Technique.BACKGROUND_OVERLAY, has_reference_assets=False)
        == Technique.GENERATED
    )


def test_a_run_starts_composed_only_with_real_images_to_compose():
    assert (
        rules.first_technique(Technique.COMPOSED, has_reference_assets=False) == Technique.GENERATED
    )
    assert (
        rules.first_technique(Technique.COMPOSED, has_reference_assets=True) == Technique.COMPOSED
    )


def test_the_creative_directors_layout_choice_stands():
    assert (
        rules.first_technique(Technique.HTML_REFERENCE, has_reference_assets=False)
        == Technique.HTML_REFERENCE
    )


# --------------------------------------------------------------- the overlay's candidate
def overlay_candidate(version: str, made_by_code: frozenset[str]) -> list:
    return rules.operation_problems(
        Operation.model_validate({"op": "candidate_ready", "version_id": version}),
        technique=Technique.BACKGROUND_OVERLAY,
        images=RunImages(
            run_versions=frozenset({V1, V2}),
            this_round=frozenset({V1, V2}),
            reference_assets=frozenset(),
            made_by_code=made_by_code,
            has_copy=True,
        ),
        cost_usd=0.0,
        limits=Limits(),
    )


def test_a_background_without_its_words_is_not_an_overlay_candidate():
    [problem] = overlay_candidate(V1, made_by_code=frozenset())
    assert "laid over in code" in problem


def test_the_background_with_its_words_laid_over_is_a_candidate():
    assert overlay_candidate(V2, made_by_code=frozenset({V2})) == []
