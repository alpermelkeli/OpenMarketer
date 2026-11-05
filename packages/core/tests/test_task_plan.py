"""Tests for the schemas of an orchestrator task: its request, its plan, its scope and report.

They reach no database. The checks of a plan's references and limits are the task agent's
rules and are tested with them; these hold the shapes: each step's request is its agent's
schema minus what a reference replaces, read just as strictly.
"""

import uuid
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from openmarketer_core.orchestrator.task.plan import (
    MAX_TASK_STEPS,
    STUDIO_FIELDS_REPLACED,
    WRITING_FIELDS_REPLACED,
    StudioStep,
    StudioStepRequest,
    TaskAgentName,
    TaskPlan,
    TaskPlanChange,
    TaskRequest,
    TaskScope,
    WritingStep,
    WritingStepRequest,
)
from openmarketer_core.studio.requests import AssetRequest
from openmarketer_core.writing.brief import WritingBrief, WritingGoal, WritingPlatform

SCOPE: dict[str, Any] = {
    "max_steps": 6,
    "max_plan_changes": 2,
    "deadline_days": 7,
    "max_cost_usd": "1.00",
}
STUDIO_STEP: dict[str, Any] = {
    "id": "image",
    "agent": "studio",
    "request": {"goal": "Announce offline mode", "exact_copy": ["Offline"], "platform": "x_post"},
}
WRITING_STEP: dict[str, Any] = {
    "id": "post",
    "agent": "writing",
    "request": {"platform": "x", "goal": "introduce"},
    "depends_on": ["image"],
    "asset_from_step": "image",
}


def plan(*steps: dict[str, Any], **fields: Any) -> TaskPlan:
    return TaskPlan.model_validate(
        {"goal": "Post about offline mode", "steps": list(steps), "scope": SCOPE, **fields}
    )


def test_a_writing_step_request_is_a_brief_without_the_ids_its_references_replace():
    assert set(WritingStepRequest.model_fields) == (
        set(WritingBrief.model_fields) - WRITING_FIELDS_REPLACED
    )


def test_a_studio_step_request_is_an_asset_request_without_reference_assets():
    assert set(StudioStepRequest.model_fields) == (
        set(AssetRequest.model_fields) - STUDIO_FIELDS_REPLACED
    )


def test_each_kind_of_step_is_named_by_an_agent_a_task_may_start():
    assert {"research", "writing", "studio"} == {agent.value for agent in TaskAgentName}


def test_a_plan_reads_each_step_by_its_agent():
    read = plan(STUDIO_STEP, WRITING_STEP)

    assert [type(step) for step in read.steps] == [StudioStep, WritingStep]


def test_a_step_of_an_agent_a_task_may_not_start_is_refused():
    with pytest.raises(ValidationError):
        plan({**STUDIO_STEP, "agent": "publisher"})


def test_a_writing_step_cannot_carry_an_asset_id():
    step = {**WRITING_STEP, "request": {**WRITING_STEP["request"], "asset": str(uuid.uuid4())}}

    with pytest.raises(ValidationError):
        plan(STUDIO_STEP, step)


def test_a_studio_step_cannot_carry_a_reference():
    with pytest.raises(ValidationError):
        plan({**STUDIO_STEP, "asset_from_step": "image"})


def test_a_writing_step_may_rest_on_a_report_at_hand_by_its_label():
    step = plan({**WRITING_STEP, "research_from_report": "report-2"}).steps[0]

    assert isinstance(step, WritingStep)
    assert step.research_from_report == "report-2"


@pytest.mark.parametrize(
    "label", ["report-0", "report-one", "4f3072a0-1e4d-4b24-8ee1-2a06f5e7a4f0"]
)
def test_a_report_label_is_report_then_a_number(label: str):
    with pytest.raises(ValidationError):
        plan({**WRITING_STEP, "research_from_report": label})


def test_a_writing_step_rests_on_one_research_source():
    step = {**WRITING_STEP, "research_from_step": "research", "research_from_report": "report-1"}

    with pytest.raises(ValidationError):
        plan(step)


def test_a_writing_step_keeps_the_rules_of_a_brief():
    step = {**WRITING_STEP, "request": {"platform": "x", "goal": "announce"}}

    with pytest.raises(ValidationError):
        plan(STUDIO_STEP, step)


def test_an_instagram_writing_step_is_read_before_its_image_is_resolved():
    step = {**WRITING_STEP, "request": {"platform": "instagram", "goal": "introduce"}}

    request = plan(step).steps[0].request
    assert isinstance(request, WritingStepRequest)
    assert request.platform is WritingPlatform.INSTAGRAM


def test_an_instagram_writing_step_becomes_a_brief_only_with_its_image():
    request = WritingStepRequest(platform=WritingPlatform.INSTAGRAM, goal=WritingGoal.INTRODUCE)
    asset = uuid.uuid4()

    assert request.brief_with(asset=asset, research_run_id=None).asset == asset
    with pytest.raises(ValidationError):
        request.brief_with(asset=None, research_run_id=None)


def test_a_writing_step_becomes_a_brief_with_the_ids_code_resolved():
    asset, research = uuid.uuid4(), uuid.uuid4()
    request = WritingStepRequest.model_validate(WRITING_STEP["request"])

    brief = request.brief_with(asset=asset, research_run_id=research)

    assert (brief.asset, brief.research_run_id, brief.goal.value) == (asset, research, "introduce")


def test_a_studio_step_becomes_the_asset_request_it_asks_for():
    request = StudioStepRequest.model_validate(STUDIO_STEP["request"])

    assert request.asset_request() == AssetRequest.model_validate(STUDIO_STEP["request"])


def test_a_step_id_is_used_once_in_a_plan():
    with pytest.raises(ValidationError):
        plan(STUDIO_STEP, {**WRITING_STEP, "id": "image"})


def test_a_step_waits_for_another_step_once():
    with pytest.raises(ValidationError):
        plan(STUDIO_STEP, {**WRITING_STEP, "depends_on": ["image", "image"]})


def test_a_step_id_is_a_label_not_free_text():
    with pytest.raises(ValidationError):
        plan({**STUDIO_STEP, "id": "Step One"})


def test_a_plan_has_at_least_one_step_and_at_most_the_ceiling():
    with pytest.raises(ValidationError):
        plan()
    with pytest.raises(ValidationError):
        plan(*({**STUDIO_STEP, "id": f"image-{n}"} for n in range(MAX_TASK_STEPS + 1)))


@pytest.mark.parametrize(
    "field",
    [
        {"max_steps": MAX_TASK_STEPS + 1},
        {"max_steps": 0},
        {"max_plan_changes": -1},
        {"deadline_days": 0},
        {"max_cost_usd": "1000"},
        {"max_cost_usd": "0.001"},
    ],
)
def test_a_scope_beyond_its_ceilings_is_refused(field):
    with pytest.raises(ValidationError):
        TaskScope.model_validate({**SCOPE, **field})


def test_a_scope_reads_its_cost_as_a_decimal():
    assert TaskScope.model_validate(SCOPE).max_cost_usd == Decimal("1.00")


def test_a_request_needs_the_operators_words_and_the_chats_understanding():
    with pytest.raises(ValidationError):
        TaskRequest.model_validate({"request": "Write a post"})


def test_a_plan_change_keeps_the_plan_it_replaced():
    replaced = plan(STUDIO_STEP)

    change = TaskPlanChange(reason="The image was rejected.", replaced=replaced)

    assert TaskPlanChange.model_validate(change.model_dump(mode="json")) == change
