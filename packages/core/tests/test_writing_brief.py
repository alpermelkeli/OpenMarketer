"""Tests for the brief of a writing run: what each goal carries, and how every field is bounded."""

import pytest
from pydantic import ValidationError

from openmarketer_core.writing.brief import (
    DEFAULT_VARIANTS,
    MAX_ANGLE_CHARS,
    MAX_VARIANTS,
    WritingBrief,
    WritingGoal,
    WritingPlatform,
)


def brief(**fields) -> WritingBrief:
    return WritingBrief.model_validate({"platform": "x", "goal": "introduce", **fields})


def test_an_introduction_needs_only_the_platform_and_the_goal():
    introduction = brief()

    assert introduction.platform is WritingPlatform.X
    assert introduction.goal is WritingGoal.INTRODUCE
    assert introduction.variants == DEFAULT_VARIANTS


def test_an_announcement_names_its_feature():
    assert brief(goal="announce", feature="offline-sync").feature == "offline-sync"


def test_an_announcement_without_a_feature_is_refused():
    with pytest.raises(ValidationError):
        brief(goal="announce")


@pytest.mark.parametrize("goal", ["introduce", "angle"])
def test_a_feature_is_refused_for_any_other_goal(goal):
    with pytest.raises(ValidationError):
        brief(goal=goal, feature="offline-sync", angle="Why we built it")


def test_a_feature_is_a_profile_feature_id():
    with pytest.raises(ValidationError):
        brief(goal="announce", feature="Offline Sync")


def test_an_angle_brief_carries_the_angle_stripped():
    assert brief(goal="angle", angle="  Why we built it\nin a weekend  ").angle == (
        "Why we built it\nin a weekend"
    )


@pytest.mark.parametrize("goal", ["introduce", "announce"])
def test_an_angle_is_refused_for_any_other_goal(goal):
    with pytest.raises(ValidationError):
        brief(goal=goal, feature="offline-sync" if goal == "announce" else None, angle="x")


@pytest.mark.parametrize("angle", ["", "   ", "a" * (MAX_ANGLE_CHARS + 1)])
def test_an_empty_or_long_angle_is_refused(angle):
    with pytest.raises(ValidationError):
        brief(goal="angle", angle=angle)


@pytest.mark.parametrize("control", ["\x00", "\r", "\t", "\x1b"])
def test_an_angle_holds_no_control_character_but_a_line_break(control):
    with pytest.raises(ValidationError):
        brief(goal="angle", angle=f"Why{control}we built it")


@pytest.mark.parametrize("variants", [0, MAX_VARIANTS + 1])
def test_the_number_of_variants_is_bounded(variants):
    with pytest.raises(ValidationError):
        brief(variants=variants)


def test_an_unknown_field_is_refused():
    with pytest.raises(ValidationError):
        brief(image="auto")


def test_a_platform_other_than_x_is_refused():
    with pytest.raises(ValidationError):
        brief(platform="linkedin")
