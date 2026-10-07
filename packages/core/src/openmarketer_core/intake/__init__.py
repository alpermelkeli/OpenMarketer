"""Intake: the first stage of the repository analysis pipeline.

Clone read-only, blank out secrets, then hand later stages a ``RepoFiles``
view that cannot read excluded files.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from openmarketer_core.intake.errors import ExcludedFileError, IntakeError
from openmarketer_core.intake.files import Exclusion, RepoFiles
from openmarketer_core.intake.git import Snapshot, clone
from openmarketer_core.intake.secrets import SecretFinding, scan_and_redact

__all__ = [
    "ExcludedFileError",
    "Exclusion",
    "IntakeError",
    "IntakeResult",
    "RepoFiles",
    "SecretFinding",
    "Snapshot",
    "run_intake",
]


@dataclass(frozen=True)
class IntakeResult:
    snapshot: Snapshot
    files: RepoFiles
    findings: list[SecretFinding]


def run_intake(source: str, dest: Path, *, token: str | None = None) -> IntakeResult:
    """Clone ``source`` into ``dest`` and return a safe view of its files."""
    snapshot = clone(source, dest, token=token)
    findings = scan_and_redact(snapshot.root)
    files = RepoFiles(snapshot.root, secret_files={f.file for f in findings if not f.redacted})
    return IntakeResult(snapshot=snapshot, files=files, findings=findings)
