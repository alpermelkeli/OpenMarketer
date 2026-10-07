"""Secret scan with gitleaks.

Runs before any file is read by a model. Findings never carry the secret
itself, only where it is. The scan ignores allowlists shipped inside the
repository (``.gitleaks.toml``, ``.gitleaksignore``, ``gitleaks:allow``
comments): the analysed repository must not be able to switch the scan off.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from openmarketer_core.intake.errors import IntakeError

SCAN_TIMEOUT_S = 300
DEFAULT_RULES = "[extend]\nuseDefault = true\n"


@dataclass(frozen=True)
class SecretFinding:
    file: str  # path relative to the repository root
    rule_id: str
    start_line: int
    end_line: int


def scan(root: Path) -> list[SecretFinding]:
    """Scan the working tree under ``root`` and return where secrets were found."""
    root = root.resolve()
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
                    "--redact",
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
            raise IntakeError(f"secret scan failed: {done.stderr.strip()[-500:]}")
        raw = json.loads(report.read_text() or "[]")

    findings: set[SecretFinding] = set()
    for item in raw:
        path = Path(item["File"])
        if not path.is_absolute():
            path = root / path
        try:
            rel = path.resolve().relative_to(root)
        except ValueError as e:
            raise IntakeError(f"secret scan reported a path outside the repository: {path}") from e
        findings.add(
            SecretFinding(
                file=rel.as_posix(),
                rule_id=item["RuleID"],
                start_line=item["StartLine"],
                end_line=item["EndLine"],
            )
        )
    return sorted(findings, key=lambda f: (f.file, f.start_line, f.rule_id))
