"""Running the analyzer on a golden case and keeping what it produced.

The analyzer is reached through ``analyze_repository``, the operation the
worker runs: clone the pinned commit, scan it, run the extractors, draft the
profile. Nothing of the analyzer's inside is used, so what is measured is what
a user gets.

A run that ends without a profile is a result, not a failure of the benchmark:
the clone failing, the model provider failing and the analyzer giving up are
each recorded as how the run ended. Anything else is a defect and is raised.

While the clone still exists, the lines the draft cites are read through
``RepoFiles`` and kept, bounded in size, for the judge. This module does not
ask the judge and computes no score.
"""

from __future__ import annotations

import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openmarketer_core.llm import ChatModel, ChatReply, LLMError
from openmarketer_core.llm_config import ConfigError
from openmarketer_core.profile import Evidence, ProductProfile
from openmarketer_core.repository_analysis.analyzer_agent import AnalysisError, Limits
from openmarketer_core.repository_analysis.extraction import Extractor
from openmarketer_core.repository_analysis.intake import ExcludedFileError, IntakeError, RepoFiles
from openmarketer_core.repository_analysis.pipeline import analyze_repository, failure_message
from openmarketer_evaluation.cases import GoldenCase
from openmarketer_evaluation.claims import claims_of
from openmarketer_evaluation.results import (
    AnalyzerRun,
    CitedClaim,
    CitedEvidence,
    Excerpt,
    RunEnding,
)

# How much of what a claim cites is kept for the judge.
MAX_EXCERPT_LINES = 80  # per piece of evidence
MAX_LINE_CHARS = 300
MAX_CLAIM_CHARS = 6000  # over all the evidence of one claim

_ENDING_OF = {
    IntakeError: RunEnding.CLONE_FAILED,
    AnalysisError: RunEnding.NO_PROFILE,
    LLMError: RunEnding.MODEL_FAILED,
    ConfigError: RunEnding.NOT_CONFIGURED,
}


@dataclass(frozen=True)
class AnalyzerSetup:
    """What every analyzer run of a benchmark is given."""

    model: ChatModel
    extractors: Sequence[Extractor]
    limits: Limits


@dataclass
class MeteredModel:
    """A ``ChatModel`` that counts the calls passing through it and what they cost.

    A run that ends without a profile says nothing about what it spent; the
    calls it made do.
    """

    inner: ChatModel
    calls: int = 0
    cost_usd: float = 0.0
    models: list[str] = field(default_factory=list)

    def chat(
        self,
        role: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
    ) -> ChatReply:
        reply = self.inner.chat(role, messages, tools=tools, tool_choice=tool_choice)
        self.calls += 1
        self.cost_usd += reply.cost_usd
        if reply.model and reply.model not in self.models:
            self.models.append(reply.model)
        return reply

    @property
    def model(self) -> str | None:
        """The model that answered last, the way the analyzer reports its model."""
        return self.models[-1] if self.models else None


def _numbered_lines(files: RepoFiles, evidence: Evidence) -> list[str] | None:
    """The cited lines with their numbers, or ``None`` when the file cannot be read."""
    try:
        lines = files.read_text(evidence.file).splitlines()
    except (ExcludedFileError, FileNotFoundError, IsADirectoryError):
        return None
    start, end = 1, len(lines)
    if evidence.lines:
        first, _, last = evidence.lines.partition("-")
        start, end = int(first), int(last or first)
    return [
        f"{number}: {line[:MAX_LINE_CHARS]}"
        for number, line in enumerate(lines[start - 1 : end], start)
    ]


def excerpt(files: RepoFiles, evidence: Evidence, *, max_chars: int) -> Excerpt:
    """The lines ``evidence`` points at, cut after ``MAX_EXCERPT_LINES`` lines or ``max_chars``.

    Evidence without a line range means the whole file, so its head is taken.
    """
    numbered = _numbered_lines(files, evidence)
    if numbered is None:
        return Excerpt(file=evidence.file, lines=evidence.lines, text="", complete=False)
    kept: list[str] = []
    used = 0
    for line in numbered[:MAX_EXCERPT_LINES]:
        if used + len(line) + 1 > max_chars:
            break
        kept.append(line)
        used += len(line) + 1
    return Excerpt(
        file=evidence.file,
        lines=evidence.lines,
        text="\n".join(kept),
        complete=len(kept) == len(numbered),
    )


def cited_claims(files: RepoFiles, profile: ProductProfile) -> list[CitedClaim]:
    """Each claim of ``profile`` that cites evidence, with the cited lines.

    All the evidence of one claim shares ``MAX_CLAIM_CHARS``, first come first served.
    """
    cited: list[CitedClaim] = []
    for claim in claims_of(profile):
        if not claim.evidence:
            continue
        excerpts: list[Excerpt] = []
        left = MAX_CLAIM_CHARS
        for evidence in claim.evidence:
            taken = excerpt(files, evidence, max_chars=left)
            left -= len(taken.text)
            excerpts.append(taken)
        cited.append(CitedClaim(key=claim.key, statement=claim.statement, excerpts=excerpts))
    return cited


def run_analyzer(
    case: GoldenCase, run: int, setup: AnalyzerSetup
) -> tuple[AnalyzerRun, CitedEvidence]:
    """Analyse the pinned commit of ``case`` once and return what came of it.

    The clone lives in a temporary folder for the length of the call. No access
    token is offered: a golden case is a public repository.
    """
    metered = MeteredModel(setup.model)
    record = AnalyzerRun(
        case=case.name,
        run=run,
        repository=case.repository,
        commit=case.commit,
        ending=RunEnding.PROFILE,
    )
    cited = CitedEvidence(case=case.name, run=run)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="openmarketer-eval-") as tmp:
        workdir = Path(tmp)
        try:
            done = analyze_repository(
                case.repository,
                workdir / "repo",
                model=metered,
                extractors=setup.extractors,
                limits=setup.limits,
                commit=case.commit,
            )
        except (IntakeError, AnalysisError, LLMError, ConfigError) as failure:
            record.ending = next(e for kind, e in _ENDING_OF.items() if isinstance(failure, kind))
            record.error = failure_message(failure, workdir)
        else:
            record.profile = done.analysis.profile
            record.steps = done.analysis.steps
            record.tool_calls = done.analysis.tool_calls
            record.notes = done.analysis.notes
            cited.claims = cited_claims(done.intake.files, done.analysis.profile)
    record.seconds = round(time.monotonic() - started, 1)
    record.model = metered.model
    record.cost_usd = metered.cost_usd
    record.model_calls = metered.calls
    return record, cited
