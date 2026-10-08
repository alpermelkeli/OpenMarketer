"""OpenMarketer command line."""

from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Annotated

import typer

from openmarketer_core.analyzer import AnalysisError, Limits, analyze
from openmarketer_core.db.evidence_store import SavedAnalysis, local_workspace_id, save_analysis
from openmarketer_core.db.session import DatabaseError, session_factory, transaction
from openmarketer_core.extraction import ExtractedFact, discover_extractors, run_extractors
from openmarketer_core.intake import IntakeError, RepositoryTokens, Snapshot, run_intake
from openmarketer_core.llm import LLMError, RouterChatModel
from openmarketer_core.llm_config import ConfigError
from openmarketer_core.profile import ProductProfile

app = typer.Typer(no_args_is_help=True, add_completion=False)

DATABASE_HINT = "is the dev stack running and migrated? (`make up`, `make migrate`)"

SaveRun = Callable[[Snapshot, Sequence[ExtractedFact], ProductProfile], SavedAnalysis]


def _say(message: str) -> None:
    print(message, file=sys.stderr)


def _failure(message: str) -> typer.Exit:
    _say(f"error: {message}")
    return typer.Exit(1)


def _open_store() -> SaveRun:
    """Connect to the database of DATABASE_URL and return how to save a run in it.

    The connection is tried here, before any repository is cloned or model is
    called, so a database that cannot take the result does not waste a run.
    """
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise _failure("--save needs DATABASE_URL (see .env.example)")
    try:
        sessions = session_factory(database_url)
        with transaction(sessions) as session:
            workspace_id = local_workspace_id(session)
    except DatabaseError as e:
        raise _failure(f"database: {e}\n  {DATABASE_HINT}") from e

    def save_run(
        snapshot: Snapshot, facts: Sequence[ExtractedFact], profile: ProductProfile
    ) -> SavedAnalysis:
        with transaction(sessions) as session:
            return save_analysis(
                session, workspace_id=workspace_id, snapshot=snapshot, facts=facts, profile=profile
            )

    return save_run


@app.callback()
def main() -> None:
    """OpenMarketer: give it your repository, it markets your app."""


@app.command("analyze")
def analyze_command(
    source: Annotated[str, typer.Argument(help="https:// repository URL or a local git folder")],
    out: Annotated[Path | None, typer.Option(help="Write the profile JSON here")] = None,
    max_cost: Annotated[
        float, typer.Option(help="Stop when the run has cost this much (USD)")
    ] = Limits.max_cost_usd,
    max_steps: Annotated[int, typer.Option(help="Maximum model turns")] = Limits.max_steps,
    save: Annotated[
        bool, typer.Option("--save", help="Store the run in the database of DATABASE_URL")
    ] = False,
) -> None:
    """Analyse a repository and print a draft Product Profile as JSON.

    Nothing is published. With --save the snapshot, the extractor facts and the
    profile are stored as a new draft version; otherwise nothing is stored.
    The profile is a draft for human review.
    """
    save_run = _open_store() if save else None
    try:
        tokens = RepositoryTokens.from_environment(os.environ)
    except IntakeError as e:
        raise _failure(str(e)) from e
    with tempfile.TemporaryDirectory(prefix="openmarketer-") as tmp:
        try:
            intake = run_intake(source, Path(tmp) / "repo", tokens=tokens)
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
            raise _failure(str(e)) from e

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

    if save_run is None:
        return
    try:
        saved = save_run(intake.snapshot, extraction.facts, result.profile)
    except DatabaseError as e:
        raise _failure(f"the profile was not stored: {e}") from e
    _say(
        f"stored: project {saved.project_id}, profile version {saved.profile_version}, "
        f"{saved.evidence_count} evidence rows"
    )
