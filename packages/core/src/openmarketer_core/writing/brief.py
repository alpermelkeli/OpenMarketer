"""What a writing run is asked for: a brief for one post on one platform.

A brief is written by a person (the API reads it strictly) and later by the planner or the
orchestrator's chat; it is stored with the run and read back strictly when the run starts
(``db/writing_runs.py``). It is data: it cannot choose the profile version, raise a limit or
change the platform's rules. This module reaches no database and no network.

The goal decides which of the two optional fields a brief carries: an announcement names the
feature it announces, an angle carries the person's own words, and an introduction carries
neither. Whether the feature exists and is live is checked against the approved profile by the
writer's ``prepare``, not here: the brief is read before the run knows which version it uses.

The brief has no image, asset or research yet: the first step of the writer writes text posts
only (``docs/status.md``).
"""

from __future__ import annotations

import unicodedata
import uuid
from enum import StrEnum
from typing import Annotated

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from openmarketer_core.profile import FeatureId

MAX_ANGLE_CHARS = 600
DEFAULT_VARIANTS = 3
MAX_VARIANTS = 5


class WritingGoal(StrEnum):
    INTRODUCE = "introduce"  # introduce the product
    ANNOUNCE = "announce"  # announce one live feature
    ANGLE = "angle"  # a direction in a person's own words


class WritingPlatform(StrEnum):
    X = "x"


def _without_control_characters(text: str) -> str:
    if any(unicodedata.category(char) == "Cc" and char != "\n" for char in text):
        raise ValueError("an angle holds no control character but a line break")
    return text


Angle = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_ANGLE_CHARS),
    AfterValidator(_without_control_characters),
]


class WritingBrief(BaseModel):
    """One post the writer is asked to write, for one platform, with how many texts to offer."""

    model_config = ConfigDict(extra="forbid")

    platform: WritingPlatform
    goal: WritingGoal
    feature: FeatureId | None = None
    angle: Angle | None = None
    variants: int = Field(default=DEFAULT_VARIANTS, ge=1, le=MAX_VARIANTS)

    @model_validator(mode="after")
    def _fields_of_the_goal(self) -> WritingBrief:
        if (self.goal is WritingGoal.ANNOUNCE) != (self.feature is not None):
            raise ValueError("an announcement names the feature it announces, and only it does")
        if (self.goal is WritingGoal.ANGLE) != (self.angle is not None):
            raise ValueError("an angle brief carries the angle, and only it does")
        return self


class StoredWritingBriefInvalid(Exception):
    """The brief stored with a writing run does not fit the schema any more."""

    def __init__(self, run_id: uuid.UUID) -> None:
        super().__init__(f"the stored brief of writing run {run_id} does not fit its schema")
        self.run_id = run_id
