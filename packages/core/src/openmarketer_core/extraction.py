"""Deterministic extraction: the interface between extractors and the core.

An extractor reads files of the analysed repository through ``RepoFiles`` and
returns facts: small, reproducible observations with the file and lines they
came from. No language model is involved. Facts are stored as evidence and the
synthesis step later builds the Product Profile from them.

Extractors are plugins. A package registers one under the
``openmarketer.extractors`` entry-point group.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import Protocol, Self, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from openmarketer_core.intake import RepoFiles

ENTRY_POINT_GROUP = "openmarketer.extractors"


class Fact(BaseModel):
    """One observation about the repository. Text values are untrusted data."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str = Field(pattern=r"^[a-z0-9_]+(\.[a-z0-9_]+)*$")  # e.g. "manifest.name"
    value: JsonValue
    file: str
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = None

    @model_validator(mode="after")
    def _line_range(self) -> Self:
        if self.end_line is not None and (
            self.start_line is None or self.end_line < self.start_line
        ):
            raise ValueError("end_line needs a start_line and must not be before it")
        return self


class ExtractedFact(Fact):
    """A fact together with the extractor that produced it."""

    extractor: str


@runtime_checkable
class Extractor(Protocol):
    name: str

    def extract(self, files: RepoFiles) -> Iterable[Fact]: ...


@dataclass(frozen=True)
class ExtractionResult:
    facts: list[ExtractedFact] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)  # extractor name -> error


def discover_extractors() -> list[Extractor]:
    """Load every installed extractor plugin, ordered by name."""
    found: list[Extractor] = []
    for entry_point in entry_points(group=ENTRY_POINT_GROUP):
        loaded = entry_point.load()
        extractor = loaded() if isinstance(loaded, type) else loaded
        if not isinstance(extractor, Extractor):
            raise TypeError(f"{entry_point.value} is not an Extractor")
        found.append(extractor)
    return sorted(found, key=lambda e: e.name)


def run_extractors(files: RepoFiles, extractors: Iterable[Extractor]) -> ExtractionResult:
    """Run each extractor. One failing extractor does not stop the others."""
    result = ExtractionResult()
    for extractor in extractors:
        try:
            facts = [
                ExtractedFact(extractor=extractor.name, **fact.model_dump())
                for fact in extractor.extract(files)
            ]
        except Exception as e:  # noqa: BLE001  (plugin code: record and continue)
            result.errors[extractor.name] = f"{type(e).__name__}: {e}"
            continue
        result.facts.extend(facts)
    return result
