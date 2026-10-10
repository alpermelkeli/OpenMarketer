"""What a benchmark run leaves behind: its records and the folder they are kept in.

One folder per benchmark run::

    <results>/<benchmark id>/
      config.json                   models of the roles, limits, the code's commit, the cases
      raw/<case>/run-<n>.json       what the analyzer produced, or how the attempt ended
      verdicts/<case>/run-<n>.json  the judge's verdicts
      scores.json                   every run's scores and the summary per case
      report.md                     the same, for a person
      .gitignore                    keeps the two folders below out of git
      excerpts/<case>/run-<n>.json  the lines the draft cites, as the judge was shown them
      malformed_replies/<case>/run-<n>.json  what the judge wrote where it could not be read

``raw`` and ``verdicts`` are all that scoring needs, so scores are recomputed
from the folder without a model call, and both are meant to be committed.
``excerpts`` repeats repository content, and a reply that could not be read may
quote it, so neither is for git: the store writes a ``.gitignore`` into the
folder itself, which holds wherever the results folder is put.

The records are plain data. This module computes no metric.
"""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from openmarketer_core.profile import ProductProfile
from openmarketer_evaluation.cases import LabelProvenance
from openmarketer_evaluation.scoring import ProfileScore
from openmarketer_evaluation.verdicts import MODEL_ROUTER_ROUTE, JudgeVerdicts

_RUN_FILE = re.compile(r"run-(\d+)\.json")


class ResultsError(Exception):
    """A results folder is missing, or a file in it is not what this version wrote."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------- raw output
class RunEnding(StrEnum):
    """How one attempt at analysing a case ended. Only ``profile`` can be scored."""

    PROFILE = "profile"
    NO_PROFILE = "no_profile"  # the analyzer reached a limit, or its profile was not acceptable
    MODEL_FAILED = "model_failed"  # the model provider failed or refused
    CLONE_FAILED = "clone_failed"  # the pinned commit could not be cloned or scanned
    NOT_CONFIGURED = "not_configured"  # the model configuration cannot be used


class AnalyzerRun(_Model):
    """The raw output of one analyzer run on one case.

    ``cost_usd``, ``model_calls`` and ``model`` are counted on the calls the
    analyzer made, so a run that produced no profile has them too. ``steps`` is
    what the analyzer itself reports and exists only with a profile.
    """

    case: str
    run: int
    repository: str
    commit: str
    ending: RunEnding
    error: str | None = None
    profile: ProductProfile | None = None
    model: str | None = None
    cost_usd: float = 0.0
    model_calls: int = 0
    steps: int | None = None
    tool_calls: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)  # what the analyzer corrected in code
    seconds: float = 0.0


class Excerpt(_Model):
    """The lines one piece of evidence points at, numbered, up to a size limit."""

    file: str
    lines: str | None
    text: str
    complete: bool  # False when the limit cut it short or the file could not be read


class CitedClaim(_Model):
    """A claim of the draft together with what it cites, as put to the judge."""

    key: str
    statement: str
    excerpts: list[Excerpt]


class CitedEvidence(_Model):
    case: str
    run: int
    claims: list[CitedClaim] = Field(default_factory=list)


class MalformedReply(_Model):
    about: str  # ``feature_matching``, ``evidence:<claim key>`` or ``section:<name>``
    reply: str


class MalformedReplies(_Model):
    """What the judge wrote, for one run, wherever its reply could not be read."""

    case: str
    run: int
    replies: list[MalformedReply] = Field(default_factory=list)


def malformed_replies(verdicts: JudgeVerdicts) -> MalformedReplies:
    """The replies the verdicts carry in memory only, to be stored where git does not look."""
    about = [("feature_matching", verdicts.feature_matching)]
    about += [(f"evidence:{verdict.claim}", verdict) for verdict in verdicts.evidence]
    about += [(f"section:{verdict.section}", verdict) for verdict in verdicts.sections]
    return MalformedReplies(
        case=verdicts.case,
        run=verdicts.run,
        replies=[
            MalformedReply(about=name, reply=verdict.reply)
            for name, verdict in about
            if verdict.reply is not None
        ],
    )


# ------------------------------------------------------------ configuration
class RoleModel(_Model):
    """What a role resolved to when the benchmark started."""

    role: str
    model: str
    source: str
    fallbacks: list[str]
    temperature: float
    max_tokens: int


class JudgeRoute(_Model):
    """How the judge was reached in a benchmark run.

    By default it is the judge role's model through the model router, paid with
    provider credit. On another route the role's model did not answer:
    ``requested_model`` is what was asked of that route (``None``: its own
    default) and ``role_settings_not_applied`` the role's settings it ignores.
    """

    route: str = MODEL_ROUTER_ROUTE
    spends: str = "model provider credit"
    cost_known: bool = True
    requested_model: str | None = None
    role_settings_not_applied: list[str] = Field(default_factory=list)


class CaseRecord(_Model):
    name: str
    repository: str
    commit: str
    label_provenance: LabelProvenance
    label_is_exhaustive: bool = False
    expected_sha256: str
    notes: str = ""


class AnalyzerLimits(_Model):
    max_steps: int
    max_cost_usd: float
    max_submissions: int


class BenchmarkConfig(_Model):
    benchmark_id: str
    started_at: str
    code_commit: str | None  # of the OpenMarketer checkout that ran the benchmark
    code_has_uncommitted_changes: bool | None
    runs_per_case: int
    limits: AnalyzerLimits
    max_evidence_judgements: int
    analyzer: RoleModel
    judge: RoleModel  # the judge role as configured; it answers only on the model router route
    judge_route: JudgeRoute = Field(default_factory=JudgeRoute)
    cases: list[CaseRecord]


# ------------------------------------------------------------------ scores
class RunScore(_Model):
    """One run: how it ended, what it cost, and its metrics when it produced a profile."""

    case: str
    run: int
    ending: RunEnding
    error: str | None = None
    model: str | None = None
    cost_usd: float = 0.0
    model_calls: int = 0
    steps: int | None = None
    seconds: float = 0.0
    judge_route: str = MODEL_ROUTER_ROUTE
    judge_cost_usd: float | None = 0.0  # None: the route does not say what its calls cost
    judge_calls: int = 0
    judge_models: list[str] = Field(default_factory=list)
    # True when a model that judged this run is the one that drafted it.
    judged_by_evaluated_model: bool = False
    profile: ProfileScore | None = None


# ------------------------------------------------------------------- folder
# Folders of a benchmark run that repeat repository content or raw model text.
NOT_FOR_GIT = ("excerpts", "malformed_replies")
_GITIGNORE = (
    "# Written by the evaluation. These folders repeat lines of the evaluated repositories\n"
    "# and raw model replies; the rest of this folder is small and can be committed.\n"
    + "".join(f"/{name}/\n" for name in NOT_FOR_GIT)
)


class ResultStore:
    """The folder of one benchmark run."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder

    def _keep_out_of_git(self) -> None:
        """Write the folder's ``.gitignore``, before anything it covers is written."""
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / ".gitignore").write_text(_GITIGNORE, encoding="utf-8")

    def _path(self, kind: str, case: str, run: int) -> Path:
        return self.folder / kind / case / f"run-{run}.json"

    @staticmethod
    def _write(path: Path, record: BaseModel) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(record.model_dump_json(indent=2) + "\n", encoding="utf-8")

    @staticmethod
    def _read[Record: BaseModel](path: Path, kind: type[Record]) -> Record:
        try:
            return kind.model_validate_json(path.read_bytes())
        except FileNotFoundError as e:
            raise ResultsError(f"{path} is missing") from e
        except ValidationError as e:
            raise ResultsError(f"{path} is not a {kind.__name__} this version can read") from e

    def write_config(self, config: BenchmarkConfig) -> None:
        self._keep_out_of_git()
        self._write(self.folder / "config.json", config)

    def read_config(self) -> BenchmarkConfig:
        return self._read(self.folder / "config.json", BenchmarkConfig)

    def write_run(self, run: AnalyzerRun, cited: CitedEvidence) -> None:
        self._keep_out_of_git()
        self._write(self._path("raw", run.case, run.run), run)
        self._write(self._path("excerpts", run.case, run.run), cited)

    def read_runs(self) -> list[AnalyzerRun]:
        """Every stored analyzer run, by case name and then run number."""
        paths = [
            p for p in (self.folder / "raw").glob("*/run-*.json") if _RUN_FILE.fullmatch(p.name)
        ]
        runs = [self._read(path, AnalyzerRun) for path in paths]
        return sorted(runs, key=lambda run: (run.case, run.run))

    def write_verdicts(self, verdicts: JudgeVerdicts) -> None:
        """Store the verdicts, and apart from them the replies that could not be read."""
        self._keep_out_of_git()
        self._write(self._path("verdicts", verdicts.case, verdicts.run), verdicts)
        unread = malformed_replies(verdicts)
        if unread.replies:
            self._write(self._path("malformed_replies", verdicts.case, verdicts.run), unread)

    def read_verdicts(self, case: str, run: int) -> JudgeVerdicts | None:
        """The verdicts on a run, or ``None`` when the judge was never asked about it."""
        path = self._path("verdicts", case, run)
        return self._read(path, JudgeVerdicts) if path.exists() else None

    def write_scores(self, scores: BaseModel) -> Path:
        path = self.folder / "scores.json"
        self._write(path, scores)
        return path

    def write_report(self, report: str) -> Path:
        path = self.folder / "report.md"
        path.write_text(report, encoding="utf-8")
        return path
