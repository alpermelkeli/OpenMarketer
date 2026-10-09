"""Secret scan with gitleaks.

Runs before any file is read by a model. ``scan_and_redact`` overwrites every
secret it finds in the working tree with a placeholder, so the rest of the
file stays usable (a config file with one API key is still a config file).
A file whose secret cannot be blanked out, or that still has findings
afterwards, is reported as not redacted and must be excluded by the caller.

Findings never carry the secret itself, only where it was. The scan ignores
allowlists shipped inside the repository (``.gitleaks.toml``,
``.gitleaksignore``, ``gitleaks:allow`` comments): the analysed repository
must not be able to switch the scan off.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from openmarketer_core.repository_analysis.intake.errors import IntakeError

SCAN_TIMEOUT_S = 300
DEFAULT_RULES = "[extend]\nuseDefault = true\n"
PLACEHOLDER = b"[REDACTED]"
MIN_SECRET_LENGTH = 6


@dataclass(frozen=True)
class SecretFinding:
    file: str  # path relative to the repository root
    rule_id: str
    start_line: int
    end_line: int
    redacted: bool = False  # True: the secret was blanked out and the file is safe to read


def _run_gitleaks(root: Path, *, with_secrets: bool) -> list[dict[str, Any]]:
    """Run gitleaks on ``root`` and return its raw report with repository-relative paths."""
    with tempfile.TemporaryDirectory(prefix="om-gitleaks-") as tmp:
        config = Path(tmp, "gitleaks.toml")
        config.write_text(DEFAULT_RULES)
        report = Path(tmp, "report.json")
        ignore_dir = Path(tmp, "ignore")
        ignore_dir.mkdir()
        try:
            done = subprocess.run(
                [
                    "gitleaks", "dir",
                    "--no-banner",
                    f"--redact={0 if with_secrets else 100}",
                    "--ignore-gitleaks-allow",
                    "--config", str(config),
                    "--gitleaks-ignore-path", str(ignore_dir),
                    "--report-format", "json",
                    "--report-path", str(report),
                    "--exit-code", "0",
                    "--log-level", "error",
                    str(root),
                ],
                capture_output=True,
                text=True,
                timeout=SCAN_TIMEOUT_S,
                check=False,
            )  # fmt: skip
        except FileNotFoundError as e:
            raise IntakeError("gitleaks is not installed; refusing to continue unscanned") from e
        except subprocess.TimeoutExpired as e:
            raise IntakeError(f"secret scan timed out after {SCAN_TIMEOUT_S}s") from e
        if done.returncode != 0 or not report.exists():
            raise IntakeError("secret scan failed")
        raw = json.loads(report.read_text() or "[]")

    for item in raw:
        path = Path(item["File"])
        if not path.is_absolute():
            path = root / path
        try:
            item["File"] = path.resolve().relative_to(root).as_posix()
        except ValueError as e:
            raise IntakeError("secret scan reported a path outside the repository") from e
    return raw


def _findings(raw: list[dict[str, Any]]) -> list[SecretFinding]:
    found = {
        SecretFinding(
            file=item["File"],
            rule_id=item["RuleID"],
            start_line=item["StartLine"],
            end_line=item["EndLine"],
        )
        for item in raw
    }
    return sorted(found, key=lambda f: (f.file, f.start_line, f.rule_id))


def scan(root: Path) -> list[SecretFinding]:
    """Scan the working tree under ``root`` and return where secrets were found."""
    return _findings(_run_gitleaks(root.resolve(), with_secrets=False))


def _blank_out(path: Path, secrets: set[str]) -> bool:
    """Overwrite ``secrets`` in ``path``. Line numbers are preserved. False if any is left."""
    if path.is_symlink() or not path.is_file():
        return False
    data = path.read_bytes()
    complete = True
    for secret in sorted(secrets, key=len, reverse=True):
        needle = secret.encode()
        if len(needle) < MIN_SECRET_LENGTH or needle not in data:
            complete = False  # e.g. found inside an encoded blob: cannot be located literally
            continue
        data = data.replace(needle, PLACEHOLDER + b"\n" * needle.count(b"\n"))
    path.write_bytes(data)
    return complete


def scan_and_redact(root: Path) -> list[SecretFinding]:
    """Scan ``root``, blank out the secrets found, and verify with a second scan.

    Returns the findings of the first scan. ``redacted`` is False for every
    finding in a file that is not proven clean afterwards.
    """
    root = root.resolve()
    raw = _run_gitleaks(root, with_secrets=True)
    if not raw:
        return []

    secrets_by_file: dict[str, set[str]] = {}
    for item in raw:
        secrets_by_file.setdefault(item["File"], set()).add(item.get("Secret") or "")
    unsafe = {
        file
        for file, secrets in secrets_by_file.items()
        if file.split("/")[0] == ".git" or not _blank_out(root / file, secrets)
    }
    findings = _findings(raw)
    del raw, secrets_by_file  # the only copies of the secret values

    unsafe |= {finding.file for finding in scan(root)}
    return [replace(finding, redacted=finding.file not in unsafe) for finding in findings]
