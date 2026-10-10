"""Intake: the first stage of the repository analysis pipeline.

Clone read-only, blank out secrets, then hand later stages a ``RepoFiles``
view that cannot read excluded files.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from openmarketer_core.repository_analysis.intake.credentials import NO_TOKENS, RepositoryTokens
from openmarketer_core.repository_analysis.intake.errors import ExcludedFileError, IntakeError
from openmarketer_core.repository_analysis.intake.files import Exclusion, RepoFiles
from openmarketer_core.repository_analysis.intake.git import Snapshot, clone, remote_repository_url
from openmarketer_core.repository_analysis.intake.secrets import SecretFinding, scan_and_redact

__all__ = [
    "ExcludedFileError",
    "Exclusion",
    "IntakeError",
    "IntakeResult",
    "NO_TOKENS",
    "RepoFiles",
    "RepositoryTokens",
    "SecretFinding",
    "Snapshot",
    "remote_repository_url",
    "run_intake",
]


@dataclass(frozen=True)
class IntakeResult:
    snapshot: Snapshot
    files: RepoFiles
    findings: list[SecretFinding]


def run_intake(
    source: str,
    dest: Path,
    *,
    tokens: RepositoryTokens = NO_TOKENS,
    commit: str | None = None,
) -> IntakeResult:
    """Clone ``source`` into ``dest`` and return a safe view of its files.

    ``tokens`` are the configured access tokens; one is sent only to its own host.
    With ``commit`` (a full commit id) that commit is read instead of the
    default branch.
    """
    snapshot = clone(source, dest, tokens=tokens, commit=commit)
    findings = scan_and_redact(snapshot.root)
    files = RepoFiles(snapshot.root, secret_files={f.file for f in findings if not f.redacted})
    return IntakeResult(snapshot=snapshot, files=files, findings=findings)
