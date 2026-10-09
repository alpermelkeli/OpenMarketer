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

from openmarketer_core.repository_analysis.intake import RepoFiles

ENTRY_POINT_GROUP = "openmarketer.extractors"
MANIFEST_KIND = "repo.manifest"

# Folders that hold material around the product rather than the product itself.
AUXILIARY_DIRS = frozenset({
    "test", "tests", "__tests__", "spec", "specs", "testdata", "fixtures", "__fixtures__",
    "example", "examples", "sample", "samples", "demo", "demos", "doc", "docs",
    "documentation", "benchmark", "benchmarks", "third_party", "thirdparty", "vendor",
    "vendored", "external", "scripts", "tools",
})  # fmt: skip


def is_auxiliary(path: str) -> bool:
    """Whether ``path`` lies in a test, example, documentation or vendored folder."""
    return any(part.lower() in AUXILIARY_DIRS for part in path.split("/")[:-1])


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


@dataclass(frozen=True)
class Scope:
    """The facts of one project inside the repository.

    A repository can hold several projects (an app, its command-line tool, a
    documentation site). Each folder with a project manifest is a scope, so
    their names, versions and platforms are never mixed. ``path`` is "" for
    the repository root.
    """

    path: str
    auxiliary: bool
    facts: list[ExtractedFact] = field(default_factory=list)


def _project_dir(manifest_path: str) -> str:
    parts = manifest_path.split("/")[:-1]
    if parts and parts[-1].endswith((".xcodeproj", ".xcworkspace")):
        parts.pop()  # an Xcode project bundle sits next to its sources
    return "/".join(parts)


def group_by_scope(facts: Iterable[ExtractedFact]) -> list[Scope]:
    """Group facts by the project they belong to; main projects first, then by depth."""
    facts = list(facts)
    roots = {_project_dir(f.file) for f in facts if f.kind == MANIFEST_KIND} | {""}
    scopes = {root: Scope(path=root, auxiliary=is_auxiliary(f"{root}/x")) for root in roots}

    for fact in facts:
        folder = fact.file.rsplit("/", 1)[0] if "/" in fact.file else ""
        while folder not in scopes:
            folder = folder.rsplit("/", 1)[0] if "/" in folder else ""
        scopes[folder].facts.append(fact)

    ordered = sorted(scopes.values(), key=lambda s: (s.auxiliary, s.path.count("/"), s.path))
    return [scope for scope in ordered if scope.facts]
