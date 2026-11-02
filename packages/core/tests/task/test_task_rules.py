"""Tests for the rules a task's plan keeps to, which code holds and the prompt only repeats."""

from typing import Any

from openmarketer_core.orchestrator.task.plan import TaskPlan
from openmarketer_core.orchestrator.task.task_agent.rules import (
    TaskLimits,
    needs_confirmation,
    plan_problems,
    project_of,
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
