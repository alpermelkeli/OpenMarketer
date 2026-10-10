"""Golden cases: loading them from ``evals/cases`` and checking them.

A case is a folder with two files:

- ``case.yaml``: the repository URL, the pinned commit, how the expected
  profile was made, whether it claims to list every feature of the product
  (``label_is_exhaustive``, no unless stated), and notes on what is known to
  be weak about it;
- ``expected_profile.json``: the expected Product Profile, checked against the
  schema when the case is loaded.

A case names a commit, never a branch: a branch moves, and scores of different
commits cannot be compared. Loading does not clone anything and calls no model.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from openmarketer_core.profile import ProductProfile
from openmarketer_core.repository_analysis.intake import IntakeError, remote_repository_url

CASE_FILE = "case.yaml"
EXPECTED_PROFILE_FILE = "expected_profile.json"

_COMMIT_ID = re.compile(r"[0-9a-f]{40}")
_CASE_NAME = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")


class CaseError(Exception):
    """A golden case is missing, incomplete or does not hold what it should."""


class LabelProvenance(StrEnum):
    """How the expected profile of a case was made, from weakest to strongest.

    A label that began as the analyzer's own draft shows agreement with an
    earlier run more than correctness; the report prints this next to every score.
    """

    ANALYZER_DRAFT_APPROVED_UNCHANGED = "analyzer_draft_approved_unchanged"
    ANALYZER_DRAFT_EDITED_BY_PERSON = "analyzer_draft_edited_by_person"
    # Not an analyzer draft, and not a human label either: nobody has checked it.
    WRITTEN_BY_AI_ASSISTANT_UNREVIEWED = "written_by_ai_assistant_unreviewed"
    WRITTEN_BY_PERSON = "written_by_person"


class _CaseFile(BaseModel):
    """The content of ``case.yaml``."""

    model_config = ConfigDict(extra="forbid")

    repository: str
    commit: str
    label_provenance: LabelProvenance
    # Whether the label lists every feature of the product. Unless a case says so it does
    # not, and a drafted feature the label lacks is then no error.
    label_is_exhaustive: bool = False
    notes: str = ""

    @field_validator("repository")
    @classmethod
    def _remote_repository(cls, v: str) -> str:
        try:
            return remote_repository_url(v)
        except IntakeError as e:
            raise ValueError(str(e)) from e

    @field_validator("commit")
    @classmethod
    def _full_commit_id(cls, v: str) -> str:
        if not _COMMIT_ID.fullmatch(v):
            raise ValueError(
                "must be a full commit id (40 lowercase hexadecimal characters), not a branch"
            )
        return v


@dataclass(frozen=True)
class GoldenCase:
    """A repository at one commit and the profile the analyzer should draft for it."""

    name: str
    repository: str
    commit: str
    label_provenance: LabelProvenance
    notes: str
    expected: ProductProfile
    expected_sha256: str  # of expected_profile.json, to tell when a label changed between runs
    label_is_exhaustive: bool = False  # whether the label lists every feature of the product


def _problems(error: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in e['loc']) or 'file'}: {e['msg']}" for e in error.errors()
    )


def load_case(folder: Path) -> GoldenCase:
    """Load the case in ``folder``; its name is the folder's name.

    Raises ``CaseError`` when a file is missing, when ``case.yaml`` lacks a
    field, names something that is not a full commit id or an unknown
    provenance, or when the expected profile does not match the schema.
    """
    name = folder.name
    if not _CASE_NAME.fullmatch(name):
        raise CaseError(f"case '{name}': a case name is lowercase letters, digits and dashes")
    case_path, profile_path = folder / CASE_FILE, folder / EXPECTED_PROFILE_FILE
    for path in (case_path, profile_path):
        if not path.is_file():
            raise CaseError(f"case '{name}': {path.name} is missing")
    try:
        # Every value is read as text: a commit id of digits alone must not become a number.
        written = yaml.load(case_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        described = _CaseFile.model_validate(written)
    except yaml.YAMLError as e:
        raise CaseError(f"case '{name}': {CASE_FILE} is not valid YAML") from e
    except ValidationError as e:
        raise CaseError(f"case '{name}': {CASE_FILE}: {_problems(e)}") from e
    label = profile_path.read_bytes()
    try:
        expected = ProductProfile.model_validate_json(label)
    except ValidationError as e:
        raise CaseError(f"case '{name}': {EXPECTED_PROFILE_FILE}: {_problems(e)}") from e
    return GoldenCase(
        name=name,
        repository=described.repository,
        commit=described.commit,
        label_provenance=described.label_provenance,
        label_is_exhaustive=described.label_is_exhaustive,
        notes=described.notes.strip(),
        expected=expected,
        expected_sha256=hashlib.sha256(label).hexdigest(),
    )


def load_cases(cases_dir: Path, names: Sequence[str] = ()) -> list[GoldenCase]:
    """Load the cases called ``names`` from ``cases_dir``, or all of them, in name order.

    Raises ``CaseError`` when the folder holds no case, when a name is asked
    for that is not there, or when any case fails to load.
    """
    if not cases_dir.is_dir():
        raise CaseError(f"no cases folder at {cases_dir}")
    available = sorted(p.name for p in cases_dir.iterdir() if (p / CASE_FILE).is_file())
    if not available:
        raise CaseError(f"no cases in {cases_dir}")
    unknown = sorted(set(names) - set(available))
    if unknown:
        raise CaseError(f"unknown case {', '.join(unknown)}; there is: {', '.join(available)}")
    wanted = sorted(set(names)) if names else available
    return [load_case(cases_dir / name) for name in wanted]
