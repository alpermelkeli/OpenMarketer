"""What a writing run is asked for: a brief for one post on one platform.

A brief is written by a person (the API reads it strictly) and later by the planner or the
orchestrator's chat; it is stored with the run and read back strictly when the run starts
(``db/writing_runs.py``). It is data: it cannot choose the profile version, raise a limit or
change the platform's rules. This module reaches no database and no network.

The goal decides which of the two optional fields a brief carries: an announcement names the
feature it announces, an angle carries the person's own words, and an introduction carries
neither. Whether the feature exists and is live is checked against the approved profile by the
writer's ``prepare``, not here: the brief is read before the run knows which version it uses.

A brief may also hand the writer two ready inputs, by id: ``asset``, an image of the project's
Studio library the post goes with (no image is made for it), and ``research_run_id``, a finished
research run of the project whose findings the writer may quote as data. Agents do not start
each other, so the writer never asks for either; whoever writes the brief names what exists.
That each is the project's and usable, and that the image was made for the brief's platform, is
checked when the run is requested and again by the writer's ``prepare`` (``db/writing_runs.py``,
``db/media_assets.py``), not here. A brief stored before they existed reads without them. A
post on a platform of ``PLATFORMS_WITH_IMAGE`` (Instagram) cannot be text alone, so its brief
must name an asset: that much the brief itself holds.
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

from openmarketer_core.agent_runtime.model_input import code_rule
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
    INSTAGRAM = "instagram"


# The platforms a post cannot be published on without an image: an Instagram post is an image
# with its caption. Which images fit a platform is the platform's rule (``platforms.py``).
PLATFORMS_WITH_IMAGE = frozenset({WritingPlatform.INSTAGRAM})


def _without_control_characters(text: str) -> str:
    if any(unicodedata.category(char) == "Cc" and char != "\n" for char in text):
        raise ValueError("an angle holds no control character but a line break")
    return text


Angle = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_ANGLE_CHARS),
    AfterValidator(_without_control_characters),
]
# How many texts of the post a brief may ask the writer to offer.
Variants = Annotated[int, Field(ge=1, le=MAX_VARIANTS)]


class WritingBrief(BaseModel):
    """One post the writer is asked to write, for one platform, with how many texts to offer."""

    model_config = ConfigDict(extra="forbid")

    platform: WritingPlatform
    goal: WritingGoal
    feature: FeatureId | None = None
    angle: Angle | None = None
    variants: Variants = DEFAULT_VARIANTS
    asset: uuid.UUID | None = None  # a media asset of the project's library
    research_run_id: uuid.UUID | None = None  # a succeeded research run of the project

    @model_validator(mode="after")
    def _fields_of_the_goal(self) -> WritingBrief:
        broken = brief_rule_broken(
            self.platform,
            self.goal,
            has_feature=self.feature is not None,
            has_angle=self.angle is not None,
            has_asset=self.asset is not None,
        )
        if broken is not None:
            raise code_rule(broken)
        return self


def brief_rule_broken(
    platform: WritingPlatform,
    goal: WritingGoal,
    *,
    has_feature: bool,
    has_angle: bool,
    has_asset: bool,
) -> str | None:
    """The rule of a brief's fields that these break, in code's words, or ``None``.

    The goal decides the optional fields: announce names a feature, angle carries an angle,
    introduce carries neither; a post on a platform of ``PLATFORMS_WITH_IMAGE`` names its asset.
    Each sentence says what to send, so that a model refused for it can mend its brief.
    """
    if (goal is WritingGoal.ANNOUNCE) != has_feature:
        return (
            "goal announce names the feature it announces in feature, and only goal announce "
            "has a feature: with introduce or angle, feature is null"
        )
    if (goal is WritingGoal.ANGLE) != has_angle:
        return (
            "goal angle carries the direction in angle, and only goal angle has an angle: "
            "with introduce or announce, angle is null"
        )
    if platform in PLATFORMS_WITH_IMAGE and not has_asset:
        return f"a post on {platform.value} is published with an image: name its asset"
    return None


class StoredWritingBriefInvalid(Exception):
    """The brief stored with a writing run does not fit the schema any more."""

    def __init__(self, run_id: uuid.UUID) -> None:
        super().__init__(f"the stored brief of writing run {run_id} does not fit its schema")
        self.run_id = run_id
