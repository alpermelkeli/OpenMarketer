"""OpenMarketer command line."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from collections.abc import Callable, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from openmarketer_core.db.evidence_store import SavedAnalysis, local_workspace_id, save_analysis
from openmarketer_core.db.session import DatabaseError, session_factory, transaction
from openmarketer_core.llm import ChatModel, LLMError, RouterChatModel
from openmarketer_core.llm_config import ConfigError, Resolved
from openmarketer_core.profile import ProductProfile
from openmarketer_core.repository_analysis.analyzer_agent import AnalysisError, Limits, analyze
from openmarketer_core.repository_analysis.analyzer_agent.rules import ROLE as ANALYZER_ROLE
from openmarketer_core.repository_analysis.extraction import (
    ExtractedFact,
    discover_extractors,
    run_extractors,
)
from openmarketer_core.repository_analysis.intake import (
    IntakeError,
    RepositoryTokens,
    Snapshot,
    run_intake,
)
from openmarketer_evaluation.benchmark import (
    DEFAULT_MAX_EVIDENCE_JUDGEMENTS,
    DEFAULT_RUNS_PER_CASE,
    NotLive,
    Plan,
    benchmark_config,
    plan,
    rescore,
    run_benchmark,
)
from openmarketer_evaluation.cases import CaseError, GoldenCase, load_cases
from openmarketer_evaluation.claude_code_judge import DEFAULT_TIMEOUT_S as CLAUDE_CODE_TIMEOUT_S
from openmarketer_evaluation.claude_code_judge import NOT_APPLIED as CLAUDE_CODE_NOT_APPLIED
from openmarketer_evaluation.claude_code_judge import ROUTE as CLAUDE_CODE_ROUTE
from openmarketer_evaluation.claude_code_judge import ClaudeCodeJudge
from openmarketer_evaluation.judge import ROLE as JUDGE_ROLE
from openmarketer_evaluation.judge import SameModelError, models_that_can_answer_for_both
from openmarketer_evaluation.report import render
from openmarketer_evaluation.results import JudgeRoute, ResultsError, ResultStore, RoleModel
from openmarketer_evaluation.runner import AnalyzerSetup

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


# ---------------------------------------------------------------- evaluate
def _role_model(resolved: Resolved) -> RoleModel:
    return RoleModel(
        role=resolved.role,
        model=resolved.model,
        source=resolved.source,
        fallbacks=resolved.fallbacks,
        temperature=resolved.temperature,
        max_tokens=resolved.max_tokens,
    )


class JudgeChoice(StrEnum):
    """How `evaluate` reaches the judge."""

    OPENROUTER = "openrouter"  # the judge role's model, through the model router
    CLAUDE_CODE = "claude-code"  # Claude Code's headless mode, on the operator's subscription


def _say_plan(
    planned: Plan,
    cases: Sequence[GoldenCase],
    analyzer: RoleModel,
    judge: RoleModel,
    route: JudgeRoute,
) -> None:
    _say(f"analyzer: role {analyzer.role} -> {analyzer.model} ({analyzer.source})")
    if route.route == CLAUDE_CODE_ROUTE:
        _say(
            f"judge:    Claude Code (claude -p), model "
            f"{route.requested_model or 'left to Claude Code'}; spends {route.spends}, "
            f"not model provider credit; the judge role ({judge.model}) is not used and its "
            f"{', '.join(route.role_settings_not_applied)} are not applied; cost is not recorded"
        )
    else:
        _say(f"judge:    role {judge.role} -> {judge.model} ({judge.source})")
        shared = models_that_can_answer_for_both(
            [analyzer.model, *analyzer.fallbacks], [judge.model, *judge.fallbacks]
        )
        if shared:
            _say(
                f"warning: {', '.join(shared)} can answer for both roles as a fallback; "
                "a run judged by the model that drafted it is marked in the report"
            )
    by_name = {case.name: case for case in cases}
    for case in planned.cases:
        golden = by_name[case.case]
        _say(
            f"  {case.case}: commit {golden.commit[:10]}, label {golden.label_provenance.value}, "
            f"{case.analyzer_runs} analyzer runs, at most {case.judge_calls_at_most} judge calls"
        )
    _say(
        f"plan: {planned.analyzer_runs} analyzer runs ({planned.runs_per_case} per case), "
        f"one after another; at most {planned.judge_calls_at_most} judge calls, "
        f"about {planned.judge_calls_if_like_label} if the drafts cite as much as the labels do"
    )


def _rescore(cases: Sequence[GoldenCase], folder: Path) -> None:
    store = ResultStore(folder)
    try:
        rescored = rescore(cases, store, checkout=Path.cwd())
    except ResultsError as e:
        raise _failure(str(e)) from e
    for name in rescored.changed_labels:
        _say(f"note: the expected profile of {name} has changed since that run")
    for name in rescored.without_a_case:
        _say(f"note: {name} was left out, it is not among the cases given")
    store.write_scores(rescored.scores)
    report = render(rescored.config, rescored.scores)
    _say(f"scored again without a model call: {store.write_report(report)}")
    print(report)


@app.command("evaluate")
def evaluate_command(
    case: Annotated[
        list[str] | None, typer.Option(help="A case to run; repeat it for several. Default: all")
    ] = None,
    runs: Annotated[
        int, typer.Option(min=1, help="Analyzer runs per case")
    ] = DEFAULT_RUNS_PER_CASE,
    cases_dir: Annotated[Path, typer.Option(help="Folder of the golden cases")] = Path(
        "evals/cases"
    ),
    results_dir: Annotated[
        Path, typer.Option(help="Where the folder of this benchmark run is made")
    ] = Path("evals/results"),
    rescore_run: Annotated[
        Path | None,
        typer.Option(
            "--rescore", help="Score the stored benchmark run in this folder again; calls no model"
        ),
    ] = None,
    live: Annotated[
        bool, typer.Option("--live", help="Make the model calls. They spend your credit")
    ] = False,
    max_cost: Annotated[
        float, typer.Option(help="Stop an analyzer run when it has cost this much (USD)")
    ] = Limits.max_cost_usd,
    max_steps: Annotated[
        int, typer.Option(help="Maximum model turns of an analyzer run")
    ] = Limits.max_steps,
    max_evidence_judgements: Annotated[
        int, typer.Option(min=0, help="Evidence questions put to the judge per analyzer run")
    ] = DEFAULT_MAX_EVIDENCE_JUDGEMENTS,
    judge_route: Annotated[
        JudgeChoice,
        typer.Option(
            "--judge",
            help="How the judge is reached: the judge role's model (spends credit), or "
            "Claude Code's headless mode (spends your subscription's usage)",
        ),
    ] = JudgeChoice.OPENROUTER,
    judge_model: Annotated[
        str | None,
        typer.Option(help="With --judge claude-code: the model to ask for. Default: its own"),
    ] = None,
    judge_timeout: Annotated[
        float, typer.Option(min=1, help="With --judge claude-code: seconds one question may take")
    ] = CLAUDE_CODE_TIMEOUT_S,
) -> None:
    """Measure the analyzer against the golden cases and print the report.

    Without --live nothing is spent: the command says how many analyzer runs
    and judge calls it would make and stops. With --rescore it computes the
    scores of a stored run again from its files.
    """
    try:
        cases = load_cases(cases_dir, case or ())
    except CaseError as e:
        raise _failure(str(e)) from e
    if rescore_run is not None:
        _rescore(cases, rescore_run)
        return

    try:
        chat = RouterChatModel.from_env()
        analyzer = _role_model(chat.router.resolve(ANALYZER_ROLE))
        judge = _role_model(chat.router.resolve(JUDGE_ROLE))
    except (ConfigError, OSError, KeyError) as e:
        raise _failure(f"model configuration: {e}") from e
    if judge_model is not None and judge_route is not JudgeChoice.CLAUDE_CODE:
        raise _failure("--judge-model goes with --judge claude-code; the judge role has its model")
    if judge_model is not None and (not judge_model or judge_model.startswith("-")):
        raise _failure("--judge-model must be a model name; a name does not start with a dash")
    judge_chat: ChatModel = chat
    route = JudgeRoute()
    if judge_route is JudgeChoice.CLAUDE_CODE:
        judge_chat = ClaudeCodeJudge(model=judge_model, timeout_s=judge_timeout)
        route = JudgeRoute(
            route=CLAUDE_CODE_ROUTE,
            spends="Claude Code subscription usage",
            cost_known=False,
            requested_model=judge_model,
            role_settings_not_applied=list(CLAUDE_CODE_NOT_APPLIED),
        )
    _say_plan(plan(cases, runs, max_evidence_judgements), cases, analyzer, judge, route)
    if live:
        try:
            chat.router.headers()  # fails now, not once per run, when the provider key is missing
        except ConfigError as e:
            raise _failure(str(e)) from e
        if judge_route is JudgeChoice.CLAUDE_CODE and shutil.which("claude") is None:
            raise _failure("--judge claude-code needs Claude Code (`claude`) on the PATH")

    limits = Limits(max_steps=max_steps, max_cost_usd=max_cost)
    config = benchmark_config(
        cases,
        runs_per_case=runs,
        limits=limits,
        max_evidence_judgements=max_evidence_judgements,
        analyzer=analyzer,
        judge=judge,
        checkout=Path.cwd(),
        judge_route=route,
    )
    store = ResultStore(results_dir / config.benchmark_id)
    try:
        scores = run_benchmark(
            cases,
            config=config,
            analyzer=AnalyzerSetup(model=chat, extractors=discover_extractors(), limits=limits),
            judge=judge_chat,
            store=store,
            live=live,
            progress=_say,
        )
    except SameModelError as e:
        raise _failure(str(e)) from e
    except NotLive:
        _say(
            "nothing was run. Pass --live to make these calls; "
            f"the analyzer's spend model provider credit, the judge's {route.spends}."
        )
        return
    store.write_scores(scores)
    report = render(config, scores)
    _say(f"results: {store.write_report(report).parent}")
    print(report)
