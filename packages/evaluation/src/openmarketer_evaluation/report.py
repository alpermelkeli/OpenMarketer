"""From the scores of several runs to what a person reads and a later run compares with.

A number here always says how many runs it comes from. Each metric of a case
is given as its value in every run plus the smallest, the median and the
largest; there is no average over metrics and no score of the benchmark as a
whole. How the label of a case was made, and whether it claims to be
exhaustive, is printed next to its scores, since both decide what agreement
with the label means.

Two quantities that are easily mistaken for one another are kept apart in
every table and list: a drafted ``live`` feature whose counterpart in the label
is not ``live`` (the costly error), and a drafted ``live`` feature the label
does not have (something for a person to look at).

``summarise`` builds ``scores.json`` (the file to compare a later run with);
``render`` writes the same as Markdown. Neither calls a model or reads a file.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Sequence

from pydantic import BaseModel, ConfigDict

from openmarketer_evaluation.cases import LabelProvenance
from openmarketer_evaluation.results import BenchmarkConfig, CaseRecord, RunEnding, RunScore
from openmarketer_evaluation.scoring import (
    EvidenceOutcome,
    ProfileScore,
    Share,
    partial_matches,
)
from openmarketer_evaluation.verdicts import MODEL_ROUTER_ROUTE

# What is in the folder of a benchmark run, for whoever decides what of it to commit.
ABOUT_THE_FILES = (
    "`raw/` holds the analyzer's drafts and `verdicts/` the judge's verdicts; scores are "
    "recomputed from these two.",
    "A verdict's `reason` is the judge's own short sentence about a public repository. It is "
    "a model's words, kept as written and cut to a fixed length.",
    "`excerpts/` (the cited repository lines shown to the judge) and `malformed_replies/` (what "
    "the judge wrote where it could not be read) are kept out of git by this folder's own "
    "`.gitignore`.",
)

PROVENANCE_MEANING = {
    LabelProvenance.ANALYZER_DRAFT_APPROVED_UNCHANGED: (
        "the analyzer's own draft, approved without a stored edit"
    ),
    LabelProvenance.ANALYZER_DRAFT_EDITED_BY_PERSON: (
        "the analyzer's draft, edited by a person before approval"
    ),
    LabelProvenance.WRITTEN_BY_AI_ASSISTANT_UNREVIEWED: (
        "written by an AI assistant from the repository, "
        "not reviewed by a person: not a human label"
    ),
    LabelProvenance.WRITTEN_BY_PERSON: "written by a person",
}

# What agreement with a label of each provenance cannot show.
_PROVENANCE_LIMIT = {
    LabelProvenance.ANALYZER_DRAFT_APPROVED_UNCHANGED: (
        "began as the analyzer's own draft and was approved unchanged: agreement with it "
        "measures consistency with an earlier run more than correctness"
    ),
    LabelProvenance.ANALYZER_DRAFT_EDITED_BY_PERSON: (
        "began as the analyzer's own draft: where the person changed nothing, agreement "
        "measures consistency with an earlier run more than correctness"
    ),
    LabelProvenance.WRITTEN_BY_AI_ASSISTANT_UNREVIEWED: (
        "was written by an AI assistant and has not been reviewed by a person: it can be "
        "wrong, and disagreement with it is a question, not an analyzer error"
    ),
    LabelProvenance.WRITTEN_BY_PERSON: "was written by a person, who can be wrong too",
}

MAX_SHOWN_REASON_CHARS = 300


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Summary(_Model):
    """One metric over the runs of a case.

    ``values`` has one entry per run, in run order; ``None`` where the run has
    no value for the metric (no profile, no usable verdict, nothing to count,
    or a metric that does not apply to the case).
    ``counted`` is how many runs ``min``, ``median`` and ``max`` come from.
    """

    values: list[float | None]
    runs: int
    counted: int
    min: float | None
    median: float | None
    max: float | None


def summary(values: Sequence[float | None]) -> Summary:
    """Summarise one metric's per-run values; runs without a value are left out, not zeroed."""
    present = [value for value in values if value is not None]
    return Summary(
        values=list(values),
        runs=len(values),
        counted=len(present),
        min=min(present) if present else None,
        median=statistics.median(present) if present else None,
        max=max(present) if present else None,
    )


class CaseSummary(_Model):
    case: str
    label_provenance: LabelProvenance
    label_is_exhaustive: bool
    runs: int
    endings: dict[str, int]  # how the runs ended, by ``RunEnding``
    metrics: dict[str, Summary]
    # Drafted ids, each listed once. The first is the costly error; the second is not an error.
    live_but_expected_not_live_in_any_run: list[str]
    live_and_not_in_label_in_any_run: list[str]


class Rescoring(_Model):
    """When and by which code stored outputs were scored again, after the run itself."""

    at: str
    code_commit: str | None
    code_has_uncommitted_changes: bool | None


class BenchmarkScores(_Model):
    """``scores.json``: what a later benchmark run is compared with."""

    benchmark_id: str
    rescored: Rescoring | None = None  # None: scored by the run itself
    limits_of_this_result: list[str]
    cases: list[CaseSummary]
    runs: list[RunScore]


def limits_of(config: BenchmarkConfig) -> list[str]:
    """What a result with this configuration does not show, said of the cases it actually has."""
    names = ", ".join(case.name for case in config.cases)
    one = len(config.cases) == 1
    limits = [
        f"The golden set is {'one repository' if one else f'{len(config.cases)} repositories'} "
        f"({names}). {'It does' if one else 'They do'} not show how the analyzer does on other "
        "repositories, stacks or shapes."
    ]
    for case in config.cases:
        limits.append(f"The label of {case.name} {_PROVENANCE_LIMIT[case.label_provenance]}.")
        if not case.label_is_exhaustive:
            limits.append(
                f"The label of {case.name} does not list every feature of the product. A "
                "drafted feature it lacks is not an error by that alone, and there is no "
                "precision for this case."
            )
    limits += [
        "Feature matching, evidence support and section agreement are a model's judgements. They "
        "vary between judge models and between the routes a judge is reached by, and can be "
        "wrong; the lists matter more than the ratios. Compare runs only when judge and route "
        "are the same.",
        "Features are matched one to one. A drafted feature that covers two expected ones "
        "matches one and leaves the other among the missed.",
        "The judge saw cited lines up to a size limit, at most "
        f"{config.max_evidence_judgements} claims per run, and was not asked whether a feature "
        "marked live is reachable by users.",
        "With one run per case, min, median and max are that run's value and say nothing about "
        "how much runs differ."
        if config.runs_per_case == 1
        else f"With {config.runs_per_case} runs per case, min, median and max describe those "
        "runs, not a distribution.",
    ]
    return limits


def _ratio(
    of_profile: Callable[[ProfileScore], Share | None],
) -> Callable[[RunScore], float | None]:
    def value(score: RunScore) -> float | None:
        found = of_profile(score.profile) if score.profile else None
        return found.ratio if found else None

    return value


def _count(of_profile: Callable[[ProfileScore], int | None]) -> Callable[[RunScore], float | None]:
    def value(score: RunScore) -> float | None:
        found = of_profile(score.profile) if score.profile else None
        return None if found is None else float(found)

    return value


# Each summarised metric: its name and how it is read from one run's score. Yes/no metrics
# are 1 or 0, so their median says what most runs did and their values say which.
METRICS: dict[str, Callable[[RunScore], float | None]] = {
    "product_name_matches": _count(lambda p: int(p.product.name_matches)),
    "product_type_matches": _count(lambda p: int(p.product.type_matches)),
    "platforms_exact": _count(lambda p: int(p.product.platforms.exact)),
    "platforms_missed": _count(lambda p: len(p.product.platforms.missed)),
    "platforms_extra": _count(lambda p: len(p.product.platforms.extra)),
    "features_drafted": _count(lambda p: sum(p.drafted_statuses.values())),
    "features_drafted_live": _count(lambda p: p.drafted_statuses.get("live", 0)),
    "features_matched": _count(lambda p: len(p.features.matching.matched) if p.features else None),
    "features_missed": _count(lambda p: len(p.features.matching.missed) if p.features else None),
    "features_not_in_label": _count(
        lambda p: len(p.features.matching.not_in_label) if p.features else None
    ),
    # The costly error, on a line of its own.
    "features_live_but_expected_not_live": _count(
        lambda p: len(p.features.live_but_expected_not_live) if p.features else None
    ),
    # Not an error: live features the label does not have.
    "features_live_and_not_in_label": _count(
        lambda p: len(p.features.live_and_not_in_label) if p.features else None
    ),
    "feature_recall": _ratio(lambda p: p.features.recall if p.features else None),
    "feature_precision_exhaustive_label_only": _ratio(
        lambda p: p.features.precision if p.features else None
    ),
    "status_agreement": _ratio(lambda p: p.features.status_agreement if p.features else None),
    "claims_with_evidence": _ratio(lambda p: p.evidence.claims_with_evidence),
    "evidence_supported": _ratio(lambda p: p.evidence.supported),
    "evidence_at_least_partly_supported": _ratio(lambda p: p.evidence.at_least_partly_supported),
    "evidence_claims_supported": _count(lambda p: len(p.evidence.supported_claims)),
    "evidence_claims_partly_supported": _count(lambda p: len(p.evidence.partly_supported)),
    "evidence_claims_not_supported": _count(lambda p: len(p.evidence.not_supported)),
    "evidence_claims_not_asked": _count(lambda p: len(p.evidence.not_asked)),
    "business_model_type_matches": _count(lambda p: int(p.sections.business_model_type.same)),
    "analytics_tools_missed": _count(lambda p: len(p.sections.analytics.missed)),
    "analytics_entries_not_names": _count(lambda p: len(p.sections.analytics.not_names)),
    "analyzer_cost_usd": lambda score: score.cost_usd,
    "analyzer_model_calls": lambda score: float(score.model_calls),
    "analyzer_seconds": lambda score: score.seconds,
    "judge_cost_usd": lambda score: score.judge_cost_usd,
    "judge_calls": lambda score: float(score.judge_calls),
}


def _listed_once(scores: Sequence[RunScore], ids: Callable[[ProfileScore], list[str]]) -> list[str]:
    seen: list[str] = []
    for score in scores:
        for feature_id in ids(score.profile) if score.profile else []:
            if feature_id not in seen:
                seen.append(feature_id)
    return seen


def summarise_case(case: CaseRecord, scores: Sequence[RunScore]) -> CaseSummary:
    """The runs of one case, metric by metric. ``scores`` are that case's, in run order."""
    endings: dict[str, int] = {}
    for score in scores:
        endings[score.ending.value] = endings.get(score.ending.value, 0) + 1
    return CaseSummary(
        case=case.name,
        label_provenance=case.label_provenance,
        label_is_exhaustive=case.label_is_exhaustive,
        runs=len(scores),
        endings=endings,
        metrics={name: summary([read(s) for s in scores]) for name, read in METRICS.items()},
        live_but_expected_not_live_in_any_run=_listed_once(
            scores,
            lambda p: (
                [m.drafted_id for m in p.features.live_but_expected_not_live] if p.features else []
            ),
        ),
        live_and_not_in_label_in_any_run=_listed_once(
            scores, lambda p: p.features.live_and_not_in_label if p.features else []
        ),
    )


def summarise(
    config: BenchmarkConfig, scores: Sequence[RunScore], rescored: Rescoring | None = None
) -> BenchmarkScores:
    """The scores of a benchmark run as the file a later run is compared with.

    ``rescored`` is given when stored outputs were scored again after the run.
    """
    ordered = sorted(scores, key=lambda score: (score.case, score.run))
    return BenchmarkScores(
        benchmark_id=config.benchmark_id,
        rescored=rescored,
        limits_of_this_result=limits_of(config),
        cases=[
            summarise_case(case, [score for score in ordered if score.case == case.name])
            for case in config.cases
        ],
        runs=ordered,
    )


# ----------------------------------------------------------------- Markdown
def cost_text(cost_usd: float | None) -> str:
    """A cost for a person to read; a cost the route does not report is said to be unknown."""
    return "cost unknown" if cost_usd is None else f"${cost_usd:.4f}"


def _number(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.0f}" if float(value).is_integer() else f"{value:.3f}".rstrip("0")


def _metric_rows(case: CaseSummary) -> list[str]:
    rows = ["| Metric | Runs counted | Min | Median | Max | Per run |", "|---|---|---|---|---|---|"]
    for name, metric in case.metrics.items():
        per_run = ", ".join(_number(value) for value in metric.values)
        rows.append(
            f"| {name} | {metric.counted} of {metric.runs} | {_number(metric.min)} "
            f"| {_number(metric.median)} | {_number(metric.max)} | {per_run} |"
        )
    return rows


def _listed(values: Sequence[str]) -> str:
    return ", ".join(values) if values else "none"


def _share(found: Share) -> str:
    ratio = "-" if found.ratio is None else _number(found.ratio)
    return f"{found.count} of {found.of} ({ratio})"


def _statuses(counts: dict[str, int]) -> str:
    return ", ".join(f"{count} {status}" for status, count in counts.items())


def _stated(value: object) -> str:
    return "not stated" if value is None else repr(value)


def _with_reasons(outcomes: Sequence[EvidenceOutcome]) -> list[str]:
    """One indented line per claim, with the judge's reason next to it."""
    return [
        f"  - {o.claim}: {o.reason[:MAX_SHOWN_REASON_CHARS] or 'no reason given'}" for o in outcomes
    ]


def _feature_lines(profile: ProfileScore) -> list[str]:
    drafted, expected = profile.drafted_statuses, profile.expected_statuses
    lines = [f"- feature statuses: drafted {_statuses(drafted)}; expected {_statuses(expected)}"]
    total = sum(drafted.values())
    if total and drafted.get("live", 0) == total:
        lines.append(
            f"- **every drafted feature is live ({total} of {total})**: the draft tells "
            "nothing apart as unreleased or unknown"
        )
    features = profile.features
    if features is None:
        state = profile.feature_matching_state.value
        return [*lines, f"- features: not scored, the judge's matching is {state}"]
    costly = [
        f"{m.drafted_id} (label: {m.expected_id} is {m.expected_status.value})"
        for m in features.live_but_expected_not_live
    ]
    missed = [f"{m.id} ({m.status.value} in the label)" for m in features.matching.missed]
    partial = [f"{p.expected_id} ~ {p.drafted_id}" for p in partial_matches(features.matching)]
    statuses = [
        f"{p.drafted_id} ({p.drafted_status.value}, expected {p.expected_status.value})"
        for p in features.status_disagreements
    ]
    lines += [
        f"- **live in the draft, not live in the label (the costly error): {_listed(costly)}**",
        f"- matched {len(features.matching.matched)}, recall {_share(features.recall)}",
        f"- missed: {_listed(missed)}",
    ]
    if profile.label_is_exhaustive and features.precision is not None:
        lines += [
            f"- precision {_share(features.precision)}",
            f"- invented (the label is exhaustive): {_listed(features.matching.not_in_label)}",
            f"- of these, live: {_listed(features.live_and_not_in_label)}",
        ]
    else:
        lines += [
            "- not in the label, which is not exhaustive (to look at, not errors; no precision): "
            f"{_listed(features.matching.not_in_label)}",
            f"- of these, live: {_listed(features.live_and_not_in_label)}",
        ]
    lines += [
        f"- partial matches: {_listed(partial)}",
        f"- status of matched features: agrees for {_share(features.status_agreement)}; "
        f"differs: {_listed(statuses)}",
    ]
    return lines


def _evidence_lines(profile: ProfileScore) -> list[str]:
    evidence = profile.evidence
    lines = [
        f"- evidence: {len(evidence.supported_claims)} supported, "
        f"{len(evidence.partly_supported)} partly supported, "
        f"{len(evidence.not_supported)} not supported; strictly supported "
        f"{_share(evidence.supported)}, "
        f"at least partly {_share(evidence.at_least_partly_supported)}",
        f"- evidence supported: {_listed(evidence.supported_claims)}",
        "- evidence partly supported (the cited lines show part of the claim), with the judge's "
        f"reason:{'' if evidence.partly_supported else ' none'}",
        *_with_reasons(evidence.partly_supported),
        "- evidence not supported (the cited lines do not show the claim), with the judge's "
        f"reason:{'' if evidence.not_supported else ' none'}",
        *_with_reasons(evidence.not_supported),
        f"- evidence the judge was not asked about (the run's limit of questions): "
        f"{_listed(evidence.not_asked)}",
        "- evidence with a malformed or failed verdict: "
        f"{_listed(evidence.without_usable_verdict)}",
    ]
    return lines


def _section_lines(profile: ProfileScore) -> list[str]:
    sections = profile.sections
    lines = [
        "- sections: "
        + "; ".join(f"{name} {presence.value}" for name, presence in sections.presence.items())
    ]
    for name, judged in sections.judged.items():
        verdict = (
            judged.agreement.value if judged.agreement else f"no verdict ({judged.state.value})"
        )
        reason = f": {judged.reason[:MAX_SHOWN_REASON_CHARS]}" if judged.reason else ""
        lines.append(f"- {name}, as judged: {verdict}{reason}")
    for name, stated in (
        ("business model type", sections.business_model_type),
        ("trial days", sections.trial_days),
        ("attribution", sections.attribution),
        ("deep links", sections.deep_links),
    ):
        verdict = "same" if stated.same else "**differs**"
        lines.append(
            f"- {name}: {verdict}; drafted {_stated(stated.drafted)}, "
            f"expected {_stated(stated.expected)}"
        )
    analytics = sections.analytics
    lines += [
        f"- palette: missed {_listed(sections.palette.missed)}; "
        f"extra {_listed(sections.palette.extra)}",
        f"- analytics tools: found {_listed(analytics.found)}; missed {_listed(analytics.missed)}; "
        f"drafted entries naming no expected tool: {_listed(analytics.extra)}",
        "- analytics entries that are not a tool's name (a defect of the draft): "
        + _listed([repr(entry) for entry in analytics.not_names]),
    ]
    return lines


def _profile_lines(profile: ProfileScore) -> list[str]:
    product = profile.product
    return [
        f"- name: drafted {product.drafted_name!r}, expected {product.expected_name!r}; "
        f"type: drafted {product.drafted_type}, expected {product.expected_type}",
        f"- platforms missed: {_listed(product.platforms.missed)}; "
        f"extra: {_listed(product.platforms.extra)}",
        *_feature_lines(profile),
        *_evidence_lines(profile),
        *_section_lines(profile),
    ]


def _run_lines(score: RunScore) -> list[str]:
    head = (
        f"### Run {score.run}: {score.ending.value}, {score.model or 'no model answered'}, "
        f"${score.cost_usd:.4f}, {score.model_calls} model calls, {score.seconds:.0f}s; "
        f"judge {_listed(score.judge_models)} via {score.judge_route}, "
        f"{cost_text(score.judge_cost_usd)}, {score.judge_calls} calls"
    )
    lines = [head, ""]
    if score.judged_by_evaluated_model:
        lines.append("- **judged by the model that drafted it: do not rely on the judged metrics**")
    if score.ending is not RunEnding.PROFILE:
        lines.append(f"- no profile: {score.error}")
    if score.profile:
        lines += _profile_lines(score.profile)
    return [*lines, ""]


def _judge_line(config: BenchmarkConfig) -> str:
    """Who judged and by which route, so that results of two routes are not read as one."""
    limit = f"at most {config.max_evidence_judgements} evidence judgements per run"
    route = config.judge_route
    if route.route == MODEL_ROUTER_ROUTE:
        return (
            f"- judge: {config.judge.model} ({config.judge.source}) via {route.route}, "
            f"fallbacks {_listed(config.judge.fallbacks)}; {limit}"
        )
    return (
        f"- judge: via **{route.route}**, model {route.requested_model or 'left to that route'}; "
        f"spends {route.spends}; cost {'recorded' if route.cost_known else 'not known'}; "
        f"settings of the judge role not applied: {_listed(route.role_settings_not_applied)}; "
        f"the role's own model ({config.judge.model}) did not answer; {limit}"
    )


def _code(commit: str | None, uncommitted: bool | None) -> str:
    return (commit or "unknown") + (" with uncommitted changes" if uncommitted else "")


def render(config: BenchmarkConfig, scores: BenchmarkScores) -> str:
    """The scores as Markdown: configuration, limits, then each case with its runs."""
    lines = [
        f"# Analyzer evaluation {config.benchmark_id}",
        "",
        f"- analyzer: {config.analyzer.model} ({config.analyzer.source}), "
        f"fallbacks {_listed(config.analyzer.fallbacks)}; "
        f"limits {config.limits.max_steps} steps, ${config.limits.max_cost_usd:.2f}",
        _judge_line(config),
        f"- runs per case: {config.runs_per_case}; run made by code "
        f"{_code(config.code_commit, config.code_has_uncommitted_changes)}; "
        f"started {config.started_at}",
    ]
    if scores.rescored:
        again = scores.rescored
        lines.append(
            f"- **scored again on {again.at}** from the stored drafts and verdicts, without a "
            f"model call, by code {_code(again.code_commit, again.code_has_uncommitted_changes)}: "
            "the scores below are that code's, not those the run itself computed"
        )
    lines += [
        "",
        "## Limits of this result",
        "",
        *[f"- {limit}" for limit in scores.limits_of_this_result],
        "",
        "## About the files of this run",
        "",
        *[f"- {note}" for note in ABOUT_THE_FILES],
        "",
    ]
    records = {case.name: case for case in config.cases}
    for case in scores.cases:
        record = records[case.case]
        endings = ", ".join(f"{count} {ending}" for ending, count in case.endings.items())
        exhaustive = "exhaustive" if case.label_is_exhaustive else "not exhaustive"
        not_in_label = (
            "the label is exhaustive, so these are invented"
            if case.label_is_exhaustive
            else "to look at, not errors"
        )
        lines += [
            f"## {case.case}",
            "",
            f"- label: **{case.label_provenance.value}** "
            f"({PROVENANCE_MEANING[case.label_provenance]}); **{exhaustive}**",
            f"- commit {record.commit}; {case.runs} run{'' if case.runs == 1 else 's'}: "
            f"{endings or 'none'}",
            "- live in the draft, not live in the label, in any run (the costly error): "
            f"**{_listed(case.live_but_expected_not_live_in_any_run)}**",
            f"- live in the draft and not in the label, in any run ({not_in_label}): "
            f"{_listed(case.live_and_not_in_label_in_any_run)}",
        ]
        if record.notes:
            lines += ["", record.notes]
        lines += ["", *_metric_rows(case), ""]
        for score in scores.runs:
            if score.case == case.case:
                lines += _run_lines(score)
    return "\n".join(lines).rstrip() + "\n"
