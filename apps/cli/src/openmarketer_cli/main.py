"""OpenMarketer command line."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Annotated

import typer

from openmarketer_core.analyzer import AnalysisError, Limits, analyze
from openmarketer_core.extraction import discover_extractors, run_extractors
from openmarketer_core.intake import IntakeError, run_intake
from openmarketer_core.llm import LLMError, RouterChatModel
from openmarketer_core.llm_config import ConfigError

app = typer.Typer(no_args_is_help=True, add_completion=False)


def _say(message: str) -> None:
    print(message, file=sys.stderr)


@app.callback()
def main() -> None:
    """OpenMarketer: give it your repository, it markets your app."""


@app.command("analyze")
def analyze_command(
    source: Annotated[str, typer.Argument(help="https:// repository URL or a local git folder")],
    out: Annotated[Path | None, typer.Option(help="Write the profile JSON here")] = None,
    max_cost: Annotated[
        float, typer.Option(help="Stop when the run has cost this much (USD)")
    ] = 0.5,
    max_steps: Annotated[int, typer.Option(help="Maximum model turns")] = 24,
) -> None:
    """Analyse a repository and print a draft Product Profile as JSON.

    Nothing is published or stored. The profile is a draft for human review.
    """
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GITLAB_TOKEN") or None
    with tempfile.TemporaryDirectory(prefix="openmarketer-") as tmp:
        try:
            intake = run_intake(source, Path(tmp) / "repo", token=token)
            redacted = sum(f.redacted for f in intake.findings)
            _say(
                f"intake: commit {intake.snapshot.commit_sha[:10]}, "
                f"{len(list(intake.files))} readable files, {redacted} secrets blanked out, "
                f"{len({f.file for f in intake.findings if not f.redacted})} files excluded"
            )
            extraction = run_extractors(intake.files, discover_extractors())
            _say(f"extractors: {len(extraction.facts)} hints")
            for name, error in extraction.errors.items():
                _say(f"  extractor {name} failed: {error}")

            result = analyze(
                intake.files,
                RouterChatModel.from_env(),
                facts=extraction.facts,
                limits=Limits(max_steps=max_steps, max_cost_usd=max_cost),
            )
        except (IntakeError, AnalysisError, LLMError, ConfigError) as e:
            _say(f"error: {e}")
            raise typer.Exit(1) from e

    _say(
        f"analyzer: {result.model}, {result.steps} steps, "
        f"{len(result.tool_calls)} tool calls, ${result.cost_usd:.4f}"
    )
    for note in result.notes:
        _say(f"  corrected: {note}")
    text = result.profile.model_dump_json(indent=2)
    if out:
        out.write_text(text + "\n")
        _say(f"profile written to {out}")
    else:
        print(text)
