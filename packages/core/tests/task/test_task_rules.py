"""Tests for the rules a task's plan keeps to, which code holds and the prompt only repeats."""

from typing import Any

from openmarketer_core.orchestrator.task.plan import TaskPlan
from openmarketer_core.orchestrator.task.task_agent.rules import (
    TaskLimits,
    needs_confirmation,
    plan_problems,
    project_of,
    step_outcome,
)
from openmarketer_core.profile import ProductProfile

PROJECT = project_of(
    ProductProfile.model_validate(
        {
            "product": {"name": "Acme Notes", "type": "b2b_saas"},
            "features": [
                {"id": "offline-sync", "description": "Offline sync.", "status": "live"},
                {"id": "ai-summary", "description": "Summaries.", "status": "unreleased"},
            ],
            "brand": {"do_not_mention": ["BetaCorp"]},
        }
    )
)
SCOPE = {"max_steps": 4, "max_plan_changes": 2, "deadline_days": 7, "max_cost_usd": 1}


def plan_of(*steps: dict[str, Any], **scope: Any) -> TaskPlan:
    return TaskPlan.model_validate(
        {"goal": "A post.", "steps": list(steps), "scope": {**SCOPE, **scope}}
    )


def image(*copy: str) -> dict[str, Any]:
    request = {"goal": "Notes on a plane.", "exact_copy": list(copy), "platform": "x_post"}
    return {"id": "image", "agent": "studio", "request": request}


def post(**request: Any) -> dict[str, Any]:
    fields = {"platform": "x", "goal": "introduce", **request}
    return {"id": "post", "agent": "writing", "request": fields}


def test_the_operators_own_words_go_into_the_copy_however_many_lines():
    request = 'An image with "Write anywhere", "Sync later" and "Never lose a note".'
    copy = ("Write anywhere", "Sync later", "Never lose a note")

    assert plan_problems(plan_of(image(*copy)), request=request, project=PROJECT) == []


def test_copy_the_operator_did_not_give_is_at_most_two_lines():
    [problem] = plan_problems(
        plan_of(image("One", "Two", "Three")), request="An image.", project=PROJECT
    )

    assert "write at most 2 lines" in problem


def test_a_written_line_of_copy_is_short():
    [problem] = plan_problems(plan_of(image("word " * 15)), request="An image.", project=PROJECT)

    assert "longer than 60 characters" in problem


def test_the_copy_never_names_a_term_the_brand_says_not_to_mention():
    [problem] = plan_problems(
        plan_of(image("Faster than BetaCorp")), request="An image.", project=PROJECT
    )

    assert "BetaCorp" in problem and "not to mention" in problem


def test_a_post_announces_a_live_feature_only():
    [problem] = plan_problems(
        plan_of(post(goal="announce", feature="ai-summary")), request="A post.", project=PROJECT
    )

    assert "not a live feature" in problem


def test_a_post_needs_an_approved_profile():
    [problem] = plan_problems(plan_of(post()), request="A post.", project={})

    assert "no approved Product Profile" in problem


def instagram_post(*, asset_from_step: str | None = "image") -> dict[str, Any]:
    step = post(platform="instagram")
    return step if asset_from_step is None else {**step, "asset_from_step": asset_from_step}


def image_for(platform: str) -> dict[str, Any]:
    step = image("Write anywhere")
    return {**step, "request": {**step["request"], "platform": platform}}


def test_an_instagram_post_takes_an_image_made_for_instagram():
    plan = plan_of(image_for("instagram_portrait"), instagram_post())

    assert plan_problems(plan, request="Write anywhere", project=PROJECT) == []


def test_an_instagram_post_without_an_image_step_is_refused():
    [problem] = plan_problems(
        plan_of(instagram_post(asset_from_step=None)), request="A post.", project=PROJECT
    )

    assert "published with an image" in problem and "asset_from_step" in problem


def test_an_instagram_post_with_an_image_made_for_x_is_refused():
    plan = plan_of(image_for("x_post"), instagram_post())

    [problem] = plan_problems(plan, request="Write anywhere", project=PROJECT)

    assert "x_post" in problem and "instagram_post" in problem


def test_an_x_post_needs_no_image_and_may_take_one_made_for_any_platform():
    with_image = plan_of(image_for("og_image"), {**post(), "asset_from_step": "image"})

    assert plan_problems(plan_of(post()), request="A post.", project=PROJECT) == []
    assert plan_problems(with_image, request="Write anywhere", project=PROJECT) == []


def test_a_step_waits_only_for_steps_before_it():
    waiting = {**post(), "depends_on": ["image"]}

    [problem] = plan_problems(
        plan_of(waiting, image("Write anywhere")), request="Write anywhere", project=PROJECT
    )

    assert "not a step before it" in problem


def test_a_first_plan_is_confirmed_when_it_can_spend_or_goes_beyond_the_defaults():
    limits = TaskLimits()

    assert needs_confirmation(plan_of(image()), limits=limits)
    assert needs_confirmation(plan_of(post(), max_cost_usd=2), limits=limits)
    assert not needs_confirmation(plan_of(post()), limits=limits)


def test_a_change_is_confirmed_when_it_raises_the_scope_in_force():
    accepted = plan_of(post()).model_dump(mode="json")

    assert needs_confirmation(plan_of(post(), max_steps=5), limits=TaskLimits(), accepted=accepted)
    assert not needs_confirmation(plan_of(post()), limits=TaskLimits(), accepted=accepted)


# What the worker's runs answer a run action with: the projection of each run's ``finish``,
# with the id of the run the runtime made.
def test_a_studio_runs_projection_gives_its_asset_and_whether_it_waits_for_a_person():
    answered = {
        "asset_id": "a1",
        "version_id": "v1",
        "needs_review": True,
        "rounds": 2,
        "cost_usd": 0.12,
        "run_id": "r1",
    }

    assert step_outcome("studio", answered) == {
        "asset_id": "a1",
        "final_version_id": "v1",
        "needs_review": True,
        "cost_usd": 0.12,
    }


def test_a_writing_runs_projection_gives_its_post_and_how_it_ended_with_its_cost_as_text():
    answered = {"status": "rejected", "content_item_id": "c1", "cost_usd": "0.031", "run_id": "r"}

    outcome = step_outcome("writing", answered)

    assert (outcome["content_item_id"], outcome["post_status"]) == ("c1", "rejected")
    assert outcome["cost_usd"] == 0.031


def test_a_research_runs_projection_gives_how_many_findings_its_report_holds():
    answered = {"report_id": "p1", "findings": 3, "run_id": "r1"}

    assert step_outcome("research", answered) == {"findings": 3, "statements": [], "cost_usd": 0.0}
