"""Tests for the creative studio's rules: what follows a round, what an operation may name."""

import json
import subprocess
import sys

from openmarketer_core.agent_runtime.model_input import read_strict_json
from openmarketer_core.studio.brand_kit import EMPTY_BRAND_KIT, BrandKit
from openmarketer_core.studio.image_checks import COPY, WRONG_SIZE
from openmarketer_core.studio.platforms import Platform, platform_spec
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
    [problem] = problems({"op": "render_html", "html": "<p>x</p>"})
    assert "not an operation of the technique generated" in problem


def test_a_program_is_an_operation_of_every_technique():
    for technique in Technique:
        assert problems({"op": "run_python", "code": "pass"}, technique) == []


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


def test_an_operation_without_its_fields_is_told_which_and_where_they_go():
    assert problems({"op": "edit_image"}) == [
        "edit_image needs source_version_id: the version id of the image of this run to change",
        "edit_image needs instruction: put what to change in `instruction`, not in `note`",
    ]


def test_a_page_put_in_the_note_is_refused_with_where_the_page_goes():
    [problem] = problems(
        {"op": "render_html", "note": "A page with the headline on top."}, Technique.COMPOSED
    )
    assert problem == (
        "render_html needs html: the whole page goes in `html`, not in `note` or a description"
    )


def test_an_edit_whose_instruction_is_in_its_note_is_read_with_the_note_as_its_instruction():
    sent = {
        "op": "edit_image",
        "source_version_id": V1,
        "note": "Erase all lettering so code can print the exact words.",
    }
    operation = read_strict_json(Operation, sent)
    assert operation.instruction == "Erase all lettering so code can print the exact words."
    assert operation.note == ""
    assert problems(sent) == []


def test_a_note_is_never_read_as_a_page_a_program_or_a_prompt():
    for op in ("render_html", "run_python", "generate_image"):
        operation = read_strict_json(Operation, {"op": op, "note": "the content"})
        assert (operation.html, operation.code, operation.prompt) == ("", "", "")


def test_the_operation_tool_tells_the_fields_each_operation_needs():
    tool = rules.operation_tool()["function"]
    needed = "edit_image: source_version_id, instruction; render_html: html; run_python: code"
    assert needed in tool["description"]
    assert needed in tool["parameters"]["properties"]["op"]["description"]
    assert (
        "Never the content of another field"
        in (tool["parameters"]["properties"]["note"]["description"])
    )


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


def test_a_backgrounds_placement_names_the_lines_by_number_and_quotes_none_of_the_words():
    placement = (
        'Leave the top half calm: "ÖNCE" goes top left, SONRA top right, and the tagline '
        "“Her gün daha iyi” runs along the bottom."
    )

    prompt = rules.generation_prompt(
        "A split frame.",
        ["ÖNCE", "SONRA"],
        Technique.BACKGROUND_OVERLAY,
        copy_placement=placement,
    )

    assert "ÖNCE" not in prompt and "SONRA" not in prompt and "Her gün" not in prompt
    assert "line 1 goes top left, line 2 top right" in prompt
    assert "the tagline the words runs along the bottom" in prompt


def test_a_placement_given_line_by_line_keeps_only_the_positions_for_a_background():
    where = rules.positions_only("ÖNCE: top left\nSONRA: top right", ["ÖNCE", "SONRA"])
    assert where == "line 1: top left\nline 2: top right"


def test_a_generated_image_keeps_the_words_of_its_placement():
    prompt = rules.generation_prompt(
        "A frame.", ["ÖNCE"], Technique.GENERATED, copy_placement='"ÖNCE" top left'
    )
    assert 'Where the words go: "ÖNCE" top left' in prompt


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


# ----------------------------------------------------------------- repository images
def info(image_id: str, kind: str, path: str) -> rules.RepositoryImageInfo:
    return rules.RepositoryImageInfo(id=image_id, path=path, kind=kind, width=512, height=512)


def test_repository_images_are_shown_icons_first_numbered_and_bounded():
    images = [info(f"id-{n}", "screenshot", f"shots/{n:02}.png") for n in range(30)]
    images.append(info("the-icon", "icon", "icon.png"))

    listed = rules.repository_images_data(images)

    assert len(listed) == rules.MAX_REPOSITORY_IMAGES
    assert listed[0] == {
        "number": 1,
        "id": "the-icon",
        "kind": "icon",
        "path": "icon.png",
        "width": 512,
        "height": 512,
    }
    assert [image["number"] for image in listed] == list(range(1, 25))


def test_a_briefs_references_must_be_of_the_list_and_named_once():
    listed = rules.repository_images_data([info(V1, "icon", "icon.png")])
    brief = rules.Brief(
        goal="g",
        visual_concept="v",
        technique=Technique.GENERATED,
        reference_images=[
            rules.ReferenceImage(id=V1, use="corner"),
            rules.ReferenceImage(id="1", use="again"),
            rules.ReferenceImage(id=V2, use="unknown"),
        ],
    )

    assert rules.brief_problems(brief, listed) == [
        "reference_images names an id that is not of repository_images",
        "reference_images names an image more than once",
    ]


def test_a_brief_may_name_no_image_when_the_repository_has_none():
    brief = rules.Brief(
        goal="g",
        visual_concept="v",
        technique=Technique.GENERATED,
        reference_images=[rules.ReferenceImage(id=V1, use="corner")],
    )

    assert rules.brief_problems(brief, []) == [
        "reference_images must be empty: the product's repository has no images"
    ]


def test_reference_lines_say_what_each_reference_is_for_in_the_order_they_are_sent():
    listed = rules.repository_images_data(
        [info(V1, "icon", "icon.png"), info(V2, "screenshot", "shot.png")]
    )
    brief = {"reference_images": [{"id": V2, "use": "screenshot inside a phone frame"}]}

    lines = rules.reference_lines(
        rules.References(version_ids=["v"], asset_ids=["a"], repository_image_ids=[V1, V2]),
        technique=Technique.GENERATED,
        brief=brief,
        listed=listed,
    )

    assert lines.splitlines()[:4] == [
        "Reference image 1: an earlier image of this run",
        "Reference image 2: an image the request gave to follow",
        "Reference image 3: the product's real app icon",
        "Reference image 4: screenshot inside a phone frame",
    ]
    assert "do not redraw" in lines


def test_no_reference_adds_no_line():
    lines = rules.reference_lines(
        rules.References(), technique=Technique.GENERATED, brief={}, listed=[]
    )

    assert lines == ""


def test_an_operation_may_name_only_the_runs_repository_images():
    images = RunImages(
        run_versions=frozenset(),
        this_round=frozenset(),
        reference_assets=frozenset(),
        repository_images=frozenset({V1}),
    )
    named = Operation(op="run_python", code="print(1)", input_repository_image_ids=[V2])

    problems = rules.operation_problems(
        named, technique=Technique.COMPOSED, images=images, cost_usd=0.0, limits=Limits()
    )

    assert problems == ["a repository image is named by an id that is not of repository_images"]


# ------------------------------------------------------------------ html_reference's steps
def layout_problems(operation: dict, *, renders: int = 0, by_image_model=frozenset()) -> list:
    return rules.operation_problems(
        Operation.model_validate(operation),
        technique=Technique.HTML_REFERENCE,
        images=RunImages(
            run_versions=frozenset({V1, V2}),
            this_round=frozenset({V1, V2}),
            reference_assets=frozenset(),
            renders_this_round=renders,
            made_by_image_model=by_image_model,
        ),
        cost_usd=0.0,
        limits=Limits(),
    )


def test_a_layout_may_be_rendered_again_once_to_fix_it():
    assert layout_problems({"op": "render_html", "html": "<p>x</p>"}, renders=1) == []


def test_a_third_render_of_the_layout_is_refused():
    [problem] = layout_problems({"op": "render_html", "html": "<p>x</p>"}, renders=2)
    assert "call generate_image" in problem


def test_a_rendered_layout_is_not_a_candidate():
    [problem] = layout_problems({"op": "candidate_ready", "version_id": V1})
    assert "only the reference" in problem


def test_what_the_image_model_made_from_the_layout_is_a_candidate():
    candidate = {"op": "candidate_ready", "version_id": V2}
    assert layout_problems(candidate, by_image_model=frozenset({V2})) == []


def test_the_studio_is_told_what_a_program_is_good_for_in_any_technique():
    prompt = rules.STUDIO_SYSTEM_PROMPT
    for use in ("composite the product's real icon", "redraw", "device", "contrast", "layout"):
        assert use in prompt


def test_a_program_made_on_the_image_models_image_is_a_layouts_candidate():
    problems_of = rules.operation_problems(
        Operation(op="candidate_ready", version_id=V2),
        technique=Technique.HTML_REFERENCE,
        images=RunImages(
            run_versions=frozenset({V1, V2}),
            this_round=frozenset({V1, V2}),
            reference_assets=frozenset(),
            made_by_image_model=frozenset({V1}),
            composited_on_image_model=frozenset({V2}),
        ),
        cost_usd=0.0,
        limits=Limits(),
    )
    assert problems_of == []


def round_state(technique: Technique, operations: list, exact_copy=("Hello",)) -> dict:
    return {"technique": technique.value, "operations": operations, "exact_copy": list(exact_copy)}


def test_the_next_step_after_a_render_is_the_image_model_with_the_render():
    state = round_state(
        Technique.HTML_REFERENCE, [{"op": "render_html", "version_id": V1, "ok": True}]
    )
    step = rules.next_step(state)
    assert step.startswith("generate_image") and V1 in step


def test_the_next_step_of_an_overlay_after_its_background_is_the_words_in_code():
    state = round_state(
        Technique.BACKGROUND_OVERLAY, [{"op": "generate_image", "version_id": V1, "ok": True}]
    )
    assert rules.next_step(state).startswith("run_python")


def test_a_generated_image_has_no_fixed_next_step():
    assert rules.next_step(round_state(Technique.GENERATED, [])) == ""


ICON_ID = "33333333-3333-4333-8333-333333333333"


def with_the_real_icon(state: dict) -> dict:
    return {
        **state,
        "repository_images": [{"id": ICON_ID, "kind": "icon"}],
        "brief": {"reference_images": [{"id": ICON_ID, "use": "app icon in the corner"}]},
    }


def test_after_an_image_models_image_the_briefs_real_icon_is_pasted_in_code():
    state = with_the_real_icon(
        round_state(Technique.GENERATED, [{"op": "generate_image", "version_id": V1, "ok": True}])
    )
    step = rules.next_step(state)
    assert "run_python" in step
    assert f"input_version_ids [{V1}]" in step
    assert f"input_repository_image_ids [{ICON_ID}]" in step


def test_a_brand_failure_with_the_real_icon_chosen_is_mended_by_pasting_it_in_code():
    spec = platform_spec(Platform.INSTAGRAM_POST)
    state = with_the_real_icon({})
    [strategy] = rules.edit_strategies(
        ["verifier:brand_fit"],
        Technique.GENERATED,
        spec,
        real_assets=rules.chosen_real_assets(state),
    )
    assert strategy.startswith("Paste the product's real icon")
    assert ICON_ID in strategy


def test_an_overlay_round_without_a_candidate_is_told_to_lay_the_words_over_in_code():
    spec = platform_spec(Platform.INSTAGRAM_POST)
    [strategy] = rules.edit_strategies([rules.NO_CANDIDATE], Technique.BACKGROUND_OVERLAY, spec)
    assert strategy.startswith("Lay the exact words over the background with run_python")


def test_the_verifier_fails_an_invented_icon_in_place_of_the_real_one_as_major():
    assert "brand_fit fails with severity major" in rules.VERIFIER_SYSTEM_PROMPT


# --------------------------------------------------------------------- the brief's words
def a_brief_with(copy_placement) -> rules.Brief:
    return rules.Brief.model_validate(
        {
            "goal": "A launch image.",
            "visual_concept": "A split frame.",
            "technique": "html_reference",
            "copy_placement": copy_placement,
        }
    )


def test_a_placement_given_line_by_line_is_read_as_text():
    brief = a_brief_with({"ÖNCE": "top left", "SONRA": "top right"})
    assert brief.copy_placement == "ÖNCE: top left\nSONRA: top right"


def test_a_placement_for_four_lines_has_room():
    assert len(a_brief_with("x" * 1400).copy_placement) == 1400


SORU = ["Soruyu çek, kasana at.", "Sınavdan önce tekrar et"]
OCR_MISSED_THE_FIRST = {
    "confirmed": ["Sınavdan önce tekrar et"],
    "unconfirmed": [{"expected": "Soruyu çek, kasana at.", "read": "soryuu cek kasana at"}],
}


def a_reading(text: str) -> dict:
    return {"copy_transcription": [{"line": 1, "text": text}]}


def test_a_reading_of_the_line_whatever_its_case_and_punctuation_matches():
    verdict = rules.copy_verdict(SORU, OCR_MISSED_THE_FIRST, a_reading("SORUYU ÇEK KASANA AT"))
    assert [line["matches"] for line in verdict] == [True]


def test_a_reading_with_a_plain_letter_for_a_special_one_does_not_match():
    verdict = rules.copy_verdict(SORU, OCR_MISSED_THE_FIRST, a_reading("Soruyu cek, kasana at."))
    assert [line["matches"] for line in verdict] == [False]


def test_a_reading_with_two_letters_swapped_does_not_match():
    verdict = rules.copy_verdict(SORU, OCR_MISSED_THE_FIRST, a_reading("Soryuu çek, kasana at."))
    assert [line["matches"] for line in verdict] == [False]


def test_a_line_ocr_confirmed_is_not_judged_by_the_reading():
    verdict = rules.copy_verdict(SORU, OCR_MISSED_THE_FIRST, a_reading("Soruyu çek, kasana at."))
    assert [line["line"] for line in verdict] == [1]


def test_a_check_stored_before_ocr_was_evidence_has_no_line_to_read():
    stored = {"passed": True, "missing": [], "misspelled": []}
    assert rules.unconfirmed_lines(SORU, stored) == []
    assert rules.copy_verdict(SORU, stored, {}) == []


def test_a_verifier_report_must_read_every_line_ocr_did_not_confirm():
    report = VerifierReport.model_validate(a_report())
    assert rules.verifier_problems(report, to_transcribe=[1]) == [
        "give copy_transcription for line 1"
    ]


def test_an_unconfirmed_line_alone_has_no_edit_strategy():
    assert (
        rules.edit_strategies(
            [rules.COPY_UNCONFIRMED], Technique.GENERATED, platform_spec(Platform.INSTAGRAM_POST)
        )
        == []
    )


def test_a_long_verifier_note_is_cut_not_refused():
    report = VerifierReport.model_validate(
        {
            "criteria": [
                {"criterion": "composition", "passed": True, "severity": "none", "note": "x" * 900}
            ],
            "edit_suggestions": ["y" * 900],
        }
    )
    assert len(report.criteria[0].note) == rules.MAX_NOTE_CHARS
    assert report.criteria[0].note.endswith("…")
    assert len(report.edit_suggestions[0]) == rules.MAX_NOTE_CHARS


# ------------------------------------------------------- the paste of the real assets
def test_without_a_verifier_failure_the_real_icon_is_pasted_only_after_a_comparison():
    state = with_the_real_icon(
        round_state(Technique.GENERATED, [{"op": "generate_image", "version_id": V1, "ok": True}])
    )
    step = rules.next_step(state)
    assert "compare it with the real images" in step
    assert "A close likeness of the real icon, logo or screenshot" in step
    assert "is enough: do not paste" in step


def test_after_the_verifier_failed_brand_fit_the_real_icon_is_pasted():
    state = with_the_real_icon(
        {
            **round_state(
                Technique.GENERATED, [{"op": "generate_image", "version_id": V1, "ok": True}]
            ),
            "round": 2,
            "failures": [failed(Technique.GENERATED, "verifier:brand_fit")],
        }
    )
    step = rules.next_step(state)
    assert "the verifier found an imitation" in step
    assert "do not paste" not in step


def test_after_a_program_made_the_latest_image_no_paste_is_offered_again():
    operations = [
        {"op": "generate_image", "version_id": V1, "ok": True},
        {"op": "run_python", "version_id": V2, "ok": True},
    ]
    step = rules.next_step(with_the_real_icon(round_state(Technique.GENERATED, operations)))
    assert "paste" not in step
    assert "confirm every line of the copy is visible" in step


def test_the_studio_is_told_to_compare_before_it_pastes_a_real_image():
    assert "A close likeness (the same symbol, colours and shape" in (
        " ".join(rules.STUDIO_SYSTEM_PROMPT.split())
    )


# ------------------------------------------------------------- the images code opens
REPOSITORY_ID = "44444444-4444-4444-8444-444444444444"
RUN_IMAGES = RunImages(
    run_versions=frozenset({V1, V2}),
    this_round=frozenset({V1, V2}),
    reference_assets=frozenset(),
    repository_images=frozenset({REPOSITORY_ID}),
)


def test_a_page_that_names_an_image_beside_it_is_given_it():
    page = Operation(
        op="render_html", html=f'<img src="{V1}.png"><img src="/in/{REPOSITORY_ID}.png">'
    )
    completed = rules.with_opened_inputs(page, RUN_IMAGES)
    assert completed.problems == []
    assert completed.operation.input_version_ids == [V1]
    assert completed.operation.input_repository_image_ids == [REPOSITORY_ID]


def test_an_image_a_program_lists_and_opens_is_listed_once():
    program = Operation(op="run_python", code=f"open('/in/{V1}.png')", input_version_ids=[V1])
    assert rules.with_opened_inputs(program, RUN_IMAGES).operation.input_version_ids == [V1]


def test_a_program_that_opens_more_images_than_an_operation_takes_is_refused_by_field():
    listed = [V2, V2, V2, V2]
    program = Operation(op="run_python", code=f"open('/in/{V1}.png')", input_version_ids=listed)
    [problem] = rules.with_opened_inputs(program, RUN_IMAGES).problems
    assert problem.startswith("input_version_ids would hold more than")


def test_an_operation_without_code_opens_no_image():
    generation = Operation(op="generate_image", prompt=f"/in/{V1}.png")
    assert rules.with_opened_inputs(generation, RUN_IMAGES).operation == generation


def test_a_program_that_failed_for_a_file_under_in_ends_with_how_to_mend_it():
    error = "exited with status 1: FileNotFoundError: [Errno 2] No such file: '/in/x.png'" * 20
    told = rules.with_failure_hint(error, 500)
    assert told.endswith(rules.MISSING_INPUT_HINT) and len(told) <= 500


def test_any_other_failure_is_shown_as_it_was():
    assert rules.with_failure_hint("the page did not finish", 500) == "the page did not finish"


# ------------------------------------------------------- a program's candidate looked at
def test_a_program_just_made_with_copy_is_looked_at_before_it_is_a_candidate():
    state = round_state(Technique.COMPOSED, [{"op": "run_python", "version_id": V1, "ok": True}])
    assert rules.needs_a_look(Operation(op="candidate_ready", version_id=V1), state)


def test_a_program_without_copy_is_a_candidate_at_once():
    state = round_state(
        Technique.COMPOSED, [{"op": "run_python", "version_id": V1, "ok": True}], exact_copy=()
    )
    assert not rules.needs_a_look(Operation(op="candidate_ready", version_id=V1), state)


def test_the_verifier_passes_a_close_likeness_of_the_real_icon():
    prompt = " ".join(rules.VERIFIER_SYSTEM_PROMPT.split())
    assert "A close likeness" in prompt and "passes brand_fit" in prompt
