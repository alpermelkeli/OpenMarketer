"""What a run of the creative studio is asked for: one image, its goal and its exact words.

A request is written by a person or by the orchestrator's chat on a person's behalf (the API
and the chat's request action read it strictly) and stored with the run; the worker reads it
back and checks it again when the run starts (``db/studio_runs.py``). This module reaches no
database and no network.

The image model renders the words of the image too, so the words are given here, line by line,
in ``exact_copy``, and the studio never writes or changes them: the checks read the text in a
candidate and compare it with these lines. A request with no lines asks for an image without
text. Every field is text someone else wrote, so every field is bounded, a line of copy holds
no line break or other control character, and nothing is read leniently.
``reference_asset_ids`` names assets of the same project for the studio to look at; the store
checks that they are the project's when the run is requested.
"""

from __future__ import annotations

import unicodedata
import uuid
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

from openmarketer_core.studio.platforms import Platform

MAX_GOAL_CHARS = 500
MAX_COPY_LINES = 6
MAX_COPY_LINE_CHARS = 120
MAX_STYLE_NOTES_CHARS = 500
MAX_REFERENCE_ASSETS = 4


def _without_control_characters(line: str) -> str:
    if any(unicodedata.category(char) == "Cc" for char in line):
        raise ValueError("a line of copy holds no line break or other control character")
    return line


Goal = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_GOAL_CHARS)
]
CopyLine = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_COPY_LINE_CHARS),
    AfterValidator(_without_control_characters),
]
StyleNotes = Annotated[
    str, StringConstraints(strip_whitespace=True, max_length=MAX_STYLE_NOTES_CHARS)
]


def _distinct(ids: list[uuid.UUID]) -> list[uuid.UUID]:
    if len(set(ids)) != len(ids):
        raise ValueError("a reference asset is named once")
    return ids


class AssetRequest(BaseModel):
    """One image the studio is asked to make, for one platform, with the words it must show."""

    model_config = ConfigDict(extra="forbid")

    goal: Goal
    exact_copy: list[CopyLine] = Field(default_factory=list, max_length=MAX_COPY_LINES)
    platform: Platform
    style_notes: StyleNotes = ""
    reference_asset_ids: Annotated[
        list[uuid.UUID], Field(max_length=MAX_REFERENCE_ASSETS), AfterValidator(_distinct)
    ] = Field(default_factory=list)


class StoredAssetRequestInvalid(Exception):
    """The request stored with a studio run does not fit the schema any more."""

    def __init__(self, run_id: uuid.UUID) -> None:
        super().__init__(f"the stored request of studio run {run_id} does not fit its schema")
        self.run_id = run_id
