"""Analysing one repository: intake, extractors, then the analyzer agent.

This is the whole pipeline from a repository source to a draft Product
Profile, as one operation for callers that run it in the background. It does
not store anything and does not decide where the clone lives or which model
answers: the caller passes the folder, the extractors and the chat model. Nor
does it open or empty a run's checkpoints: the caller passes them and forgets
them when the run is over.

``failure_message`` words a failed run for whoever asked for it, since the
errors of the pipeline name the folder the repository was cloned into.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from openmarketer_core.graph_checkpoints import RunCheckpoints
from openmarketer_core.llm import ChatModel, LLMError
from openmarketer_core.llm_config import ConfigError
from openmarketer_core.repository_analysis.analyzer_agent import (
    Analysis,
    AnalysisError,
    Limits,
    ResumableRun,
    analyze,
)
from openmarketer_core.repository_analysis.extraction import (
    ExtractionResult,
    Extractor,
    run_extractors,
)
from openmarketer_core.repository_analysis.intake import (
    NO_TOKENS,
    IntakeError,
    IntakeResult,
    RepositoryTokens,
    run_intake,
)

# What a run that produced no profile raises; anything else is a defect.
ANALYSIS_FAILURES = (IntakeError, AnalysisError, LLMError, ConfigError)
CLONE_PLACEHOLDER = "<clone>"


@dataclass(frozen=True)
class RepositoryAnalysis:
    """What one run produced: the snapshot it read, the extractor facts and the draft."""

    intake: IntakeResult
    extraction: ExtractionResult
    analysis: Analysis


def analyze_repository(
    source: str,
    clone_into: Path,
    *,
    model: ChatModel,
    extractors: Iterable[Extractor],
    tokens: RepositoryTokens = NO_TOKENS,
    limits: Limits | None = None,
    checkpoints: RunCheckpoints | None = None,
) -> RepositoryAnalysis:
    """Clone ``source`` into ``clone_into`` and draft a Product Profile from it.

    Of ``tokens``, only the one configured for the host of ``source`` is sent.

    With ``checkpoints`` (a run's, from ``graph_checkpoints.postgres.run_checkpoints``)
    the analyzer continues what an earlier attempt at the same run left behind,
    provided the clone is at the commit that attempt read; otherwise it starts
    over. Cloning, scanning and the extractors run on every attempt.
    ``analysis.resumption`` says how this one began.

    Raises ``IntakeError`` when the repository cannot be cloned or scanned,
    ``AnalysisError`` or ``LLMError`` when the analyzer does not produce a
    profile, and ``DatabaseError`` when the checkpoints cannot be read or written.
    """
    intake = run_intake(source, clone_into, tokens=tokens)
    extraction = run_extractors(intake.files, extractors)
    resume = (
        ResumableRun(checkpoints, commit_sha=intake.snapshot.commit_sha) if checkpoints else None
    )
    analysis = analyze(intake.files, model, facts=extraction.facts, limits=limits, resume=resume)
    return RepositoryAnalysis(intake=intake, extraction=extraction, analysis=analysis)


def failure_message(failure: Exception, workdir: Path) -> str:
    """Why an analysis failed, worded for the person who asked for it.

    git names the folder it clones into; where the service keeps its temporary
    files is not the caller's to know, so every path under ``workdir`` (the
    folder the clone was made in) is replaced.
    """
    message = str(failure)
    for path in (str(workdir.resolve()), str(workdir)):
        message = message.replace(path, CLONE_PLACEHOLDER)
    return message
