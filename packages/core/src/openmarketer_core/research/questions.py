"""What a research run is asked: its questions and the scope they are answered in.

A request is written by a person (the API reads it strictly) and stored with the
run; the worker reads it back and checks it again when the run starts. This
module reaches no database and no network: the API validates a body with it and
``db/research_runs.py`` stores what it holds.

The questions and the scope are text a person wrote, so every field is bounded
and nothing is read leniently. ``max_sources`` bounds how many pages one run
fetches at most, under the ceiling the agent's own limits keep.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

MAX_QUESTIONS = 5
MAX_QUESTION_CHARS = 300
MAX_SCOPE_ITEMS = 5
MAX_SCOPE_ITEM_CHARS = 60
MAX_TIME_WINDOW_CHARS = 40
DEFAULT_MAX_SOURCES = 8
# The most pages one run may fetch; also the ceiling of a request's ``max_sources``.
MAX_SOURCES = 12

Question = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_QUESTION_CHARS)
]
ScopeItem = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_SCOPE_ITEM_CHARS)
]
TimeWindow = Annotated[
    str, StringConstraints(strip_whitespace=True, max_length=MAX_TIME_WINDOW_CHARS)
]


class ResearchRequest(BaseModel):
    """The questions one research run answers, and the scope it answers them in."""

    model_config = ConfigDict(extra="forbid")

    questions: list[Question] = Field(min_length=1, max_length=MAX_QUESTIONS)
    topics: list[ScopeItem] = Field(default_factory=list, max_length=MAX_SCOPE_ITEMS)
    competitors: list[ScopeItem] = Field(default_factory=list, max_length=MAX_SCOPE_ITEMS)
    time_window: TimeWindow = ""
    max_sources: Annotated[int, Field(ge=1, le=MAX_SOURCES)] = DEFAULT_MAX_SOURCES


class StoredResearchRequestInvalid(Exception):
    """The request stored with a research run does not fit the schema any more."""

    def __init__(self, run_id: uuid.UUID) -> None:
        super().__init__(f"the stored request of research run {run_id} does not fit its schema")
        self.run_id = run_id
