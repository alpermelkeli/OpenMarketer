"""The benchmark as one operation: plan it, run it, or score a stored run again.

- ``plan`` says how many analyzer runs and judge calls a benchmark makes at
  most, before anything is spent.
- ``run_benchmark`` runs every case the asked number of times, one run after
  another: analyse, ask the judge, store both, score. A run that ends without
  a profile is stored and scored as such, and the benchmark goes on. It spends
  the maintainer's credit, so it starts only for a caller that says ``live=True``
  and only with a judge that is not the model it judges.
- ``rescore`` computes the scores of a stored benchmark run again from its raw
  outputs and verdicts. It takes no model, so it cannot spend anything.

The caller wires what is needed: the chat models, the extractors, the resolved
roles and the results folder. Nothing here reads the environment or names a model.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from openmarketer_core.llm import ChatModel
from openmarketer_core.repository_analysis.analyzer_agent import Limits
from openmarketer_evaluation.cases import GoldenCase
from openmarketer_evaluation.claims import claims_of
from openmarketer_evaluation.judge import (
    CALLS_BESIDE_EVIDENCE,
    check_judge_is_independent,
    judge_run,
    same_model,
)
from openmarketer_evaluation.report import BenchmarkScores, Rescoring, cost_text, summarise
from openmarketer_evaluation.results import (
    AnalyzerLimits,
    AnalyzerRun,
    BenchmarkConfig,
    CaseRecord,
    JudgeRoute,
    ResultStore,
    RoleModel,
    RunScore,
)
from openmarketer_evaluation.runner import AnalyzerSetup, run_analyzer
from openmarketer_evaluation.scoring import score_profile
from openmarketer_evaluation.verdicts import (
    MODEL_ROUTER_ROUTE,
    FeatureMatchingVerdict,
    JudgeVerdicts,
    VerdictState,
)

DEFAULT_RUNS_PER_CASE = 3  # the fewest that give a median that is not the min or the max
DEFAULT_MAX_EVIDENCE_JUDGEMENTS = 30  # per analyzer run; bounds what one run can cost in calls

Progress = Callable[[str], None]


@dataclass(frozen=True)
class CasePlan:
    case: str
    analyzer_runs: int
    judge_calls_at_most: int  # over all its runs
    judge_calls_if_like_label: int  # over all its runs, were every draft shaped like the label


@dataclass(frozen=True)
class Plan:
    """What a benchmark is about to do, in calls. No amount of money is estimated."""

    runs_per_case: int
    cases: tuple[CasePlan, ...]

    @property
    def analyzer_runs(self) -> int:
        return sum(case.analyzer_runs for case in self.cases)

    @property
    def judge_calls_at_most(self) -> int:
        return sum(case.judge_calls_at_most for case in self.cases)

    @property
    def judge_calls_if_like_label(self) -> int:
        return sum(case.judge_calls_if_like_label for case in self.cases)


def plan(cases: Sequence[GoldenCase], runs_per_case: int, max_evidence_judgements: int) -> Plan:
    """How many analyzer runs and judge calls the benchmark makes.

    Per analyzer run the judge is called at most once for the feature matching,
    once for each of the two judged sections, and ``max_evidence_judgements``
    times for evidence. The second figure assumes each draft cites evidence for
    as many claims as the case's label does; a real draft may cite more or less.
    """
    planned = []
    for case in cases:
        cited = sum(bool(claim.evidence) for claim in claims_of(case.expected))
        planned.append(
            CasePlan(
                case=case.name,
                analyzer_runs=runs_per_case,
                judge_calls_at_most=runs_per_case
                * (CALLS_BESIDE_EVIDENCE + max_evidence_judgements),
                judge_calls_if_like_label=runs_per_case
                * (CALLS_BESIDE_EVIDENCE + min(cited, max_evidence_judgements)),
            )
        )
    return Plan(runs_per_case=runs_per_case, cases=tuple(planned))


class NotLive(Exception):
    """A benchmark was asked for without saying that it may spend."""


# How long the two git commands below may take; the checkout is on this machine.
_CODE_VERSION_TIMEOUT_S = 10


def code_version(checkout: Path) -> tuple[str | None, bool | None]:
    """The commit of the OpenMarketer checkout at ``checkout``, and whether it has changes.

    ``(None, None)`` when that cannot be told (no git, not a checkout): a result
    is still worth having without it.
    """
    # Not through intake: that is for repositories somebody else wrote. This is the
    # operator's own checkout, asked two fixed read-only questions, with no shell.
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=checkout,
            capture_output=True,
            text=True,
            check=True,
            timeout=_CODE_VERSION_TIMEOUT_S,
        ).stdout.strip()
        changes = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=checkout,
            capture_output=True,
            text=True,
            check=True,
            timeout=_CODE_VERSION_TIMEOUT_S,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None, None
    return commit, bool(changes)  # fmt: skip


def benchmark_config(
    cases: Sequence[GoldenCase],
    *,
    runs_per_case: int,
    limits: Limits,
    max_evidence_judgements: int,
    analyzer: RoleModel,
    judge: RoleModel,
    checkout: Path,
    judge_route: JudgeRoute | None = None,
) -> BenchmarkConfig:
    """What a benchmark starting now is run with; its id is the time it starts (UTC).

    Without ``judge_route`` the judge is the judge role's model through the model router.
    """
    started = datetime.now(UTC)
    code_commit, uncommitted = code_version(checkout)
    return BenchmarkConfig(
        benchmark_id=started.strftime("%Y%m%dT%H%M%SZ"),
        started_at=started.isoformat(timespec="seconds"),
        code_commit=code_commit,
        code_has_uncommitted_changes=uncommitted,
        runs_per_case=runs_per_case,
        limits=AnalyzerLimits(
            max_steps=limits.max_steps,
            max_cost_usd=limits.max_cost_usd,
            max_submissions=limits.max_submissions,
        ),
        max_evidence_judgements=max_evidence_judgements,
        analyzer=analyzer,
        judge=judge,
        judge_route=judge_route or JudgeRoute(),
        cases=[case_record(case) for case in cases],
    )


def judge_model_known_beforehand(config: BenchmarkConfig) -> str | None:
    """The model that will judge, as far as it is known before the first call.

    On the model router route it is the judge role's model. On another route it
    is the model asked of that route, or ``None`` when the route is left to its
    own default; the model that then answers is recorded with every verdict and
    compared with the analyzer's in each score.
    """
    if config.judge_route.route == MODEL_ROUTER_ROUTE:
        return config.judge.model
    return config.judge_route.requested_model


def case_record(case: GoldenCase) -> CaseRecord:
    return CaseRecord(
        name=case.name,
        repository=case.repository,
        commit=case.commit,
        label_provenance=case.label_provenance,
        label_is_exhaustive=case.label_is_exhaustive,
        expected_sha256=case.expected_sha256,
        notes=case.notes,
    )


def score_run(case: GoldenCase, run: AnalyzerRun, verdicts: JudgeVerdicts | None) -> RunScore:
    """The score of one stored run: its cost and ending, and its metrics if it has a profile.

    A run with a profile and no stored verdicts is scored on what needs no judge.
    """
    score = RunScore(
        case=run.case,
        run=run.run,
        ending=run.ending,
        error=run.error,
        model=run.model,
        cost_usd=run.cost_usd,
        model_calls=run.model_calls,
        steps=run.steps,
        seconds=run.seconds,
    )
    if run.profile is None:
        return score
    if verdicts is None:
        verdicts = _not_judged(run)
    score.judge_route = verdicts.route
    score.judge_cost_usd = verdicts.cost_usd
    score.judge_calls = verdicts.answered_calls
    score.judge_models = verdicts.models
    drafted_by = run.model
    score.judged_by_evaluated_model = drafted_by is not None and any(
        same_model(drafted_by, judged_by) for judged_by in verdicts.models
    )
    score.profile = score_profile(
        case.expected, run.profile, verdicts, label_is_exhaustive=case.label_is_exhaustive
    )
    return score


def _not_judged(run: AnalyzerRun) -> JudgeVerdicts:
    """The verdicts of a run nobody judged: every question is ``not_asked``."""
    return JudgeVerdicts(
        case=run.case,
        run=run.run,
        feature_matching=FeatureMatchingVerdict(state=VerdictState.NOT_ASKED),
    )


def run_benchmark(
    cases: Sequence[GoldenCase],
    *,
    config: BenchmarkConfig,
    analyzer: AnalyzerSetup,
    judge: ChatModel,
    store: ResultStore,
    live: bool,
    progress: Progress = lambda _: None,
) -> BenchmarkScores:
    """Run every case ``config.runs_per_case`` times, one after another, and store everything.

    The model calls spend the maintainer's credit, so two things are checked
    before anything is cloned, called or written: the judge role must not
    resolve to the analyzer's model (``SameModelError``), and the caller must
    have said ``live=True`` (``NotLive``). ``live`` has no default on purpose.

    Each run's raw output and verdicts are written as soon as they exist, so an
    interrupted benchmark keeps what it paid for and ``rescore`` can finish it.
    """
    judge_model = judge_model_known_beforehand(config)
    if judge_model is not None:
        check_judge_is_independent(config.analyzer.model, judge_model)
    if not live:
        raise NotLive("the benchmark makes model calls that spend credit, and was not told it may")
    store.write_config(config)
    scores: list[RunScore] = []
    for case in cases:
        for number in range(1, config.runs_per_case + 1):
            progress(f"{case.name} run {number}/{config.runs_per_case}: analysing")
            run, cited = run_analyzer(case, number, analyzer)
            store.write_run(run, cited)
            verdicts = None
            if run.profile is not None:
                progress(f"{case.name} run {number}/{config.runs_per_case}: judging")
                verdicts = judge_run(
                    judge,
                    case=case.name,
                    run=number,
                    expected=case.expected,
                    drafted=run.profile,
                    cited=cited.claims,
                    max_evidence_judgements=config.max_evidence_judgements,
                    route=config.judge_route.route,
                    cost_known=config.judge_route.cost_known,
                )
                store.write_verdicts(verdicts)
            score = score_run(case, run, verdicts)
            progress(
                f"{case.name} run {number}/{config.runs_per_case}: {run.ending.value}, "
                f"analyzer ${run.cost_usd:.4f}, judge {cost_text(score.judge_cost_usd)}"
            )
            scores.append(score)
    return summarise(config, scores)


@dataclass(frozen=True)
class Rescored:
    config: BenchmarkConfig  # the stored one, with the cases as they are given now
    scores: BenchmarkScores
    changed_labels: tuple[str, ...]  # cases whose expected profile differs from the stored run's
    without_a_case: tuple[str, ...]  # stored cases that are no longer among the cases given


def rescore(
    cases: Sequence[GoldenCase], store: ResultStore, *, checkout: Path | None = None
) -> Rescored:
    """Score a stored benchmark run again from its raw outputs and verdicts, with no model.

    The stored drafts and verdicts are read and never changed. The cases are
    the ones given now: a corrected label takes effect, and so does what a case
    says about its label (its provenance, whether it is exhaustive);
    ``changed_labels`` names the cases whose expected profile differs. Runs of
    a case that is not given are left out and named.

    With ``checkout`` the scores record when and by which code they were
    recomputed, so that nobody takes them for the ones the run itself computed.
    """
    config = store.read_config()
    by_name = {case.name: case for case in cases}
    scores: list[RunScore] = []
    without_a_case: list[str] = []
    for run in store.read_runs():
        case = by_name.get(run.case)
        if case is None:
            if run.case not in without_a_case:
                without_a_case.append(run.case)
            continue
        scores.append(score_run(case, run, store.read_verdicts(run.case, run.run)))
    stored = [record for record in config.cases if record.name in by_name]
    changed = [r.name for r in stored if by_name[r.name].expected_sha256 != r.expected_sha256]
    config = config.model_copy(update={"cases": [case_record(by_name[r.name]) for r in stored]})
    rescored = None
    if checkout is not None:
        commit, uncommitted = code_version(checkout)
        rescored = Rescoring(
            at=datetime.now(UTC).isoformat(timespec="seconds"),
            code_commit=commit,
            code_has_uncommitted_changes=uncommitted,
        )
    return Rescored(
        config=config,
        scores=summarise(config, scores, rescored),
        changed_labels=tuple(changed),
        without_a_case=tuple(without_a_case),
    )
