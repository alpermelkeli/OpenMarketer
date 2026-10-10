"""Scoring a drafted Product Profile against the expected one.

Every metric is one function whose docstring says exactly what it counts. The
functions are pure: they take profiles and the judge's verdicts as data and
return data. They call no model and read no file, so the scores of a stored
run are recomputed without spending anything.

Metrics are kept apart on purpose. There is no overall score. The costly error
has a function and a line of its own and is never folded into a ratio: a
drafted feature marked ``live`` whose counterpart in the label is not ``live``
(``live_but_expected_not_live``), since only ``live`` features may appear in
public content. It is not to be confused with a drafted ``live`` feature the
label does not have at all (``live_and_not_in_label``): a label that does not
claim to list every feature cannot make that an error.

Whether a label is exhaustive is a fact about the case, passed in. Without it
a drafted feature the label lacks is "not in the label", not invented, and
there is no precision.

A ratio whose denominator is zero is ``None``, not 0 or 1: "nothing to find"
is not the same as "found everything". Whatever needs a verdict the judge did
not give is ``None`` too.

Not here: confidence calibration (whether confidence tracks correctness) and
comparing two benchmark runs. The rows they need are in the scores:
``EvidenceOutcome`` and ``MatchedFeature`` keep the draft's confidence.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from openmarketer_core.profile import Feature, FeatureStatus, ProductProfile
from openmarketer_evaluation.claims import SECTIONS, claims_of, is_filled_in, section_of
from openmarketer_evaluation.verdicts import (
    Agreement,
    JudgeVerdicts,
    MatchKind,
    ProposedMatch,
    Support,
    VerdictState,
)

# The free-text sections whose agreement with the expectation only a judge can tell.
JUDGED_SECTIONS = ("brand_voice", "audience")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Share(_Model):
    """``count`` out of ``of``. ``ratio`` is ``None`` when there was nothing to count."""

    count: int
    of: int
    ratio: float | None


def share(count: int, of: int) -> Share:
    return Share(count=count, of=of, ratio=count / of if of else None)


class SetComparison(_Model):
    """Two sets of values side by side: what the draft lacks and what it adds."""

    expected: list[str]
    drafted: list[str]
    missed: list[str]  # expected, not drafted
    extra: list[str]  # drafted, not expected
    exact: bool


def compare_sets(expected: Iterable[str], drafted: Iterable[str]) -> SetComparison:
    """Compare two collections as sets of exact strings; order and repeats do not count."""
    wanted, given = set(expected), set(drafted)
    return SetComparison(
        expected=sorted(wanted),
        drafted=sorted(given),
        missed=sorted(wanted - given),
        extra=sorted(given - wanted),
        exact=wanted == given,
    )


# ------------------------------------------------------------------ product
def normalised_name(name: str) -> str:
    """A product name for comparison: Unicode NFKC, case folded, runs of whitespace as one space."""
    return " ".join(unicodedata.normalize("NFKC", name).casefold().split())


def product_name_matches(expected: ProductProfile, drafted: ProductProfile) -> bool:
    """Whether the two product names are equal after ``normalised_name``.

    Nothing else is forgiven: "Notes" does not match "Notes: shared notebooks".
    The report prints both names, so a near miss is visible as one.
    """
    return normalised_name(expected.product.name) == normalised_name(drafted.product.name)


def product_type_matches(expected: ProductProfile, drafted: ProductProfile) -> bool:
    """Whether ``product.type`` is the same slug in both profiles."""
    return expected.product.type == drafted.product.type


def platforms(expected: ProductProfile, drafted: ProductProfile) -> SetComparison:
    """``product.platforms`` of both profiles as sets of slugs: missed, extra, and whether equal."""
    return compare_sets(expected.product.platforms, drafted.product.platforms)


def languages(expected: ProductProfile, drafted: ProductProfile) -> SetComparison:
    """``product.languages`` of both profiles as sets of lower-cased language tags."""
    return compare_sets(
        (tag.lower() for tag in expected.product.languages),
        (tag.lower() for tag in drafted.product.languages),
    )


# ----------------------------------------------------------------- features
class MatchedFeature(_Model):
    """An expected feature and the drafted feature counted as the same one."""

    expected_id: str
    drafted_id: str
    kind: MatchKind
    expected_status: FeatureStatus
    drafted_status: FeatureStatus
    drafted_confidence: float


class ExpectedFeature(_Model):
    """An expected feature by id, with the status the label gives it."""

    id: str
    status: FeatureStatus


class FeatureMatching(_Model):
    """Which drafted feature stands for which expected one, one to one."""

    matched: list[MatchedFeature] = Field(default_factory=list)
    missed: list[ExpectedFeature] = Field(default_factory=list)  # no drafted counterpart
    not_in_label: list[str] = Field(default_factory=list)  # drafted ids with no counterpart
    discarded: list[ProposedMatch] = Field(default_factory=list)  # proposals not used


def match_features(
    expected: ProductProfile, drafted: ProductProfile, proposed: Sequence[ProposedMatch]
) -> FeatureMatching:
    """Turn the judge's proposed pairs into a one-to-one matching of the two feature lists.

    Each expected feature is matched to at most one drafted feature and the
    other way round. Proposals of kind ``same`` are taken before ``partial``
    ones, and within a kind in the order the judge gave them: the first pair to
    claim a feature keeps it. A later pair that names a feature already matched,
    or an id neither profile has, is put in ``discarded`` and changes nothing.

    A ``partial`` pair counts as matched. It keeps its kind, so the report can
    list partial matches apart. Expected features left without a pair are
    ``missed``, drafted ones ``not_in_label``; both keep the order of their profile.

    One to one has a cost: a drafted feature that covers two expected ones
    matches one of them and leaves the other ``missed``.
    """
    expected_by_id = {feature.id: feature for feature in expected.features}
    drafted_by_id = {feature.id: feature for feature in drafted.features}
    matching = FeatureMatching()
    used_expected: set[str] = set()
    used_drafted: set[str] = set()
    in_order = [p for p in proposed if p.kind is MatchKind.SAME]
    in_order += [p for p in proposed if p.kind is MatchKind.PARTIAL]
    for pair in in_order:
        known = pair.expected_id in expected_by_id and pair.drafted_id in drafted_by_id
        if not known or pair.expected_id in used_expected or pair.drafted_id in used_drafted:
            matching.discarded.append(pair)
            continue
        used_expected.add(pair.expected_id)
        used_drafted.add(pair.drafted_id)
        drafted_feature = drafted_by_id[pair.drafted_id]
        matching.matched.append(
            MatchedFeature(
                expected_id=pair.expected_id,
                drafted_id=pair.drafted_id,
                kind=pair.kind,
                expected_status=expected_by_id[pair.expected_id].status,
                drafted_status=drafted_feature.status,
                drafted_confidence=drafted_feature.confidence,
            )
        )
    matching.missed = [
        ExpectedFeature(id=f.id, status=f.status)
        for f in expected.features
        if f.id not in used_expected
    ]
    matching.not_in_label = [f.id for f in drafted.features if f.id not in used_drafted]
    return matching


def feature_recall(matching: FeatureMatching) -> Share:
    """Expected features that have a drafted counterpart, out of all expected features."""
    return share(len(matching.matched), len(matching.matched) + len(matching.missed))


def feature_precision(matching: FeatureMatching, *, label_is_exhaustive: bool) -> Share | None:
    """Drafted features that have an expected counterpart, out of all drafted features.

    Only for a label that claims to list every feature of the product. For any
    other label this is ``None``: a drafted feature the label lacks may be real,
    so the share would say how short the label is, not how wrong the draft is.
    """
    if not label_is_exhaustive:
        return None
    return share(len(matching.matched), len(matching.matched) + len(matching.not_in_label))


def partial_matches(matching: FeatureMatching) -> list[MatchedFeature]:
    """The matched pairs the judge called ``partial`` rather than ``same``."""
    return [pair for pair in matching.matched if pair.kind is MatchKind.PARTIAL]


def status_counts(features: Sequence[Feature]) -> dict[str, int]:
    """How many of ``features`` have each status; every status is there, also with 0."""
    return {
        status.value: sum(feature.status is status for feature in features)
        for status in FeatureStatus
    }


def live_but_expected_not_live(
    drafted: ProductProfile, matching: FeatureMatching
) -> list[MatchedFeature]:
    """The costly error: drafted ``live`` features whose expected counterpart is not ``live``.

    A drafted feature is listed when it is marked ``live`` and is matched to an
    expected feature that the label marks ``unreleased`` or ``unknown``. Only
    ``live`` features may appear in public content, so each entry is something
    that could be announced although it is not released. The order is the
    draft's. A drafted feature without a counterpart is never here: see
    ``live_and_not_in_label``.
    """
    counterpart = {pair.drafted_id: pair for pair in matching.matched}
    return [
        counterpart[feature.id]
        for feature in drafted.features
        if feature.status is FeatureStatus.LIVE
        and feature.id in counterpart
        and counterpart[feature.id].expected_status is not FeatureStatus.LIVE
    ]


def live_and_not_in_label(drafted: ProductProfile, matching: FeatureMatching) -> list[str]:
    """Drafted ``live`` features that have no counterpart in the label, in the draft's order.

    Not an error by itself. With a label that is not exhaustive these are
    features for a person to look at: real ones the label left out, or made-up
    ones. With an exhaustive label they are features the product does not have,
    announced as available.
    """
    unmatched = set(matching.not_in_label)
    return [
        feature.id
        for feature in drafted.features
        if feature.status is FeatureStatus.LIVE and feature.id in unmatched
    ]


def status_agreement(matching: FeatureMatching) -> Share:
    """Matched pairs whose drafted status equals the expected status, out of all matched pairs."""
    agreeing = sum(pair.drafted_status is pair.expected_status for pair in matching.matched)
    return share(agreeing, len(matching.matched))


def status_disagreements(matching: FeatureMatching) -> list[MatchedFeature]:
    """The matched pairs whose drafted status differs from the expected status."""
    return [pair for pair in matching.matched if pair.drafted_status is not pair.expected_status]


# ----------------------------------------------------------------- evidence
class EvidenceOutcome(_Model):
    """One drafted claim: whether it cites anything, and what the judge made of what it cites."""

    claim: str
    confidence: float
    cites_evidence: bool
    judged: VerdictState
    support: Support | None = None
    reason: str = ""  # the judge's sentence, as stored with the verdict


def evidence_outcomes(drafted: ProductProfile, verdicts: JudgeVerdicts) -> list[EvidenceOutcome]:
    """Each claim of the draft with its evidence verdict, in the order of ``claims_of``.

    A claim the verdicts say nothing about is ``not_asked``.
    """
    verdict_of = {verdict.claim: verdict for verdict in verdicts.evidence}
    outcomes: list[EvidenceOutcome] = []
    for claim in claims_of(drafted):
        verdict = verdict_of.get(claim.key)
        outcomes.append(
            EvidenceOutcome(
                claim=claim.key,
                confidence=claim.confidence,
                cites_evidence=bool(claim.evidence),
                judged=verdict.state if verdict else VerdictState.NOT_ASKED,
                support=verdict.support if verdict else None,
                reason=verdict.reason if verdict else "",
            )
        )
    return outcomes


def claims_with_evidence(drafted: ProductProfile) -> Share:
    """Claims of the draft that cite at least one piece of evidence, out of all its claims.

    The claims are those of ``claims_of``: the product, each filled-in section
    and each feature. The analyzer already guarantees that cited lines exist.
    """
    claims = claims_of(drafted)
    return share(sum(bool(claim.evidence) for claim in claims), len(claims))


def _judged(outcomes: Sequence[EvidenceOutcome]) -> list[EvidenceOutcome]:
    return [o for o in outcomes if o.cites_evidence and o.judged is VerdictState.JUDGED]


def evidence_with_support(
    outcomes: Sequence[EvidenceOutcome], support: Support
) -> list[EvidenceOutcome]:
    """The claims whose cited lines got exactly the verdict ``support``, in the draft's order."""
    return [o for o in _judged(outcomes) if o.support is support]


def supported_evidence(outcomes: Sequence[EvidenceOutcome]) -> Share:
    """The strict share: claims judged ``supported``, out of the claims the judge judged.

    ``partly_supported`` does not count here. The denominator is the claims
    that cite evidence and got a usable verdict, so a claim the judge was not
    asked about, or answered unusably, lowers the denominator, not the score;
    those are listed by ``evidence_not_asked`` and ``evidence_without_usable_verdict``.
    The judge saw the cited lines up to a size limit, not whole files.
    """
    judged = _judged(outcomes)
    return share(len(evidence_with_support(outcomes, Support.SUPPORTED)), len(judged))


def at_least_partly_supported_evidence(outcomes: Sequence[EvidenceOutcome]) -> Share:
    """Claims judged ``supported`` or ``partly_supported``, out of the claims the judge judged.

    The same denominator as ``supported_evidence``. The difference between the
    two shares is the claims that say more than their cited lines show.
    """
    judged = _judged(outcomes)
    return share(sum(o.support is not Support.NOT_SUPPORTED for o in judged), len(judged))


def evidence_not_asked(outcomes: Sequence[EvidenceOutcome]) -> list[str]:
    """Keys of the claims that cite evidence the judge was never asked about.

    That is the run's limit of evidence questions at work, or a run nobody judged.
    """
    return [o.claim for o in outcomes if o.cites_evidence and o.judged is VerdictState.NOT_ASKED]


def evidence_without_usable_verdict(outcomes: Sequence[EvidenceOutcome]) -> list[str]:
    """Keys of the claims the judge was asked about and gave no usable verdict on.

    The reply was malformed, or the call failed.
    """
    unusable = (VerdictState.MALFORMED, VerdictState.FAILED)
    return [o.claim for o in outcomes if o.cites_evidence and o.judged in unusable]


# ----------------------------------------------------------- other sections
class Presence(StrEnum):
    """Whether a section is filled in, in the draft compared with the expectation."""

    BOTH_FILLED = "both_filled"
    BOTH_EMPTY = "both_empty"
    FILLED_WHERE_EXPECTED_EMPTY = "filled_where_expected_empty"
    EMPTY_WHERE_EXPECTED_FILLED = "empty_where_expected_filled"


def section_presence(expected: ProductProfile, drafted: ProductProfile) -> dict[str, Presence]:
    """For brand, audience, business model and measurement: is each filled in where expected?

    A section is filled in when any of its fields other than evidence and
    confidence is set (``claims.is_filled_in``). ``filled_where_expected_empty``
    is the draft saying something the expectation leaves open.
    """
    by_state = {
        (True, True): Presence.BOTH_FILLED,
        (False, False): Presence.BOTH_EMPTY,
        (False, True): Presence.FILLED_WHERE_EXPECTED_EMPTY,
        (True, False): Presence.EMPTY_WHERE_EXPECTED_FILLED,
    }
    return {
        name: by_state[
            is_filled_in(section_of(expected, name)), is_filled_in(section_of(drafted, name))
        ]
        for name in SECTIONS
    }


class StatedValue(_Model):
    """One field of a section in both profiles; ``None`` is "not stated"."""

    expected: Any
    drafted: Any
    same: bool


def _stated(expected: Any, drafted: Any) -> StatedValue:
    return StatedValue(expected=expected, drafted=drafted, same=expected == drafted)


def business_model_type(expected: ProductProfile, drafted: ProductProfile) -> StatedValue:
    """``business_model.type`` of both profiles; the same only when equal, ``unknown`` included."""
    return _stated(expected.business_model.type.value, drafted.business_model.type.value)


def trial_days(expected: ProductProfile, drafted: ProductProfile) -> StatedValue:
    """``business_model.trial_days`` of both profiles; the same only when equal or both absent."""
    return _stated(expected.business_model.trial_days, drafted.business_model.trial_days)


def attribution(expected: ProductProfile, drafted: ProductProfile) -> StatedValue:
    """``measurement.attribution`` of both profiles, compared by ``normalised_name``.

    Free text, so two wordings of one thing differ. What this catches reliably
    is a draft stating an attribution where the label states none, or the reverse.
    """
    said = [
        None if value is None else normalised_name(value)
        for value in (expected.measurement.attribution, drafted.measurement.attribution)
    ]
    return StatedValue(
        expected=expected.measurement.attribution,
        drafted=drafted.measurement.attribution,
        same=said[0] == said[1],
    )


def deep_links(expected: ProductProfile, drafted: ProductProfile) -> StatedValue:
    """``measurement.deep_links`` of both profiles: yes, no, or not stated; the same when equal."""
    return _stated(expected.measurement.deep_links, drafted.measurement.deep_links)


def palette(expected: ProductProfile, drafted: ProductProfile) -> SetComparison:
    """``brand.palette`` of both profiles as sets of upper-case #RRGGBB colours."""
    return compare_sets(expected.brand.palette, drafted.brand.palette)


MAX_NAME_WORDS = 4
MAX_NAME_CHARS = 40
_NOT_IN_A_NAME = ",;:()"


class AnalyticsComparison(_Model):
    """``measurement.analytics`` of both profiles: which expected tools the draft names."""

    expected: list[str]
    drafted: list[str]  # as the draft wrote them
    found: list[str]  # expected tools named in some drafted entry
    missed: list[str]  # expected tools named in none
    extra: list[str]  # drafted entries that name no expected tool
    not_names: list[str]  # drafted entries that are not a short name: a defect of the draft


def analytics_entries_that_are_not_names(drafted: ProductProfile) -> list[str]:
    """Entries of the draft's ``measurement.analytics`` that are not the short name of a tool.

    The field holds tool names. An entry is a short name when it has at most
    ``MAX_NAME_WORDS`` words, at most ``MAX_NAME_CHARS`` characters and none of
    the characters ``, ; : ( )``. Anything else is a sentence or a remark where
    a name belongs, and is listed here as written.
    """
    return [
        entry
        for entry in drafted.measurement.analytics
        if len(entry.split()) > MAX_NAME_WORDS
        or len(entry) > MAX_NAME_CHARS
        or any(character in entry for character in _NOT_IN_A_NAME)
    ]


def analytics(expected: ProductProfile, drafted: ProductProfile) -> AnalyticsComparison:
    """Which analytics tools of the label the draft names, by containment.

    Names are compared after ``normalised_name``. An expected tool is ``found``
    when its name occurs in a drafted entry as a whole phrase, that is, not
    inside a longer word; otherwise it is ``missed``. A drafted entry that
    contains no expected tool is ``extra``.

    Containment gives credit to a draft that buries the name in a sentence, and
    that is on purpose kept visible: ``not_names`` lists such entries
    (``analytics_entries_that_are_not_names``), whether or not a tool was found in them.
    """
    wanted = sorted({normalised_name(name) for name in expected.measurement.analytics})
    entries = [normalised_name(entry) for entry in drafted.measurement.analytics]

    def names(entry: str, tool: str) -> bool:
        return re.search(rf"(?<!\w){re.escape(tool)}(?!\w)", entry) is not None

    found = [tool for tool in wanted if any(names(entry, tool) for entry in entries)]
    return AnalyticsComparison(
        expected=wanted,
        drafted=list(drafted.measurement.analytics),
        found=found,
        missed=[tool for tool in wanted if tool not in found],
        extra=[
            written
            for written, entry in zip(drafted.measurement.analytics, entries, strict=True)
            if not any(names(entry, tool) for tool in wanted)
        ],
        not_names=analytics_entries_that_are_not_names(drafted),
    )


class JudgedSection(_Model):
    """The judge's verdict on a free-text section; no ``agreement`` without a usable one."""

    state: VerdictState
    agreement: Agreement | None = None
    reason: str = ""


def judged_section(verdicts: JudgeVerdicts, section: str) -> JudgedSection:
    """The judge's verdict on whether a drafted free-text section agrees with the expected one.

    ``section`` is one of ``JUDGED_SECTIONS``. ``not_asked`` when the verdicts
    say nothing about it, which is the case when one of the two sides is empty.
    """
    for verdict in verdicts.sections:
        if verdict.section == section:
            return JudgedSection(
                state=verdict.state, agreement=verdict.agreement, reason=verdict.reason
            )
    return JudgedSection(state=VerdictState.NOT_ASKED)


# ------------------------------------------------------------- one profile
class ProductScore(_Model):
    expected_name: str
    drafted_name: str
    name_matches: bool
    expected_type: str
    drafted_type: str
    type_matches: bool
    platforms: SetComparison
    languages: SetComparison


class FeatureScore(_Model):
    """Feature metrics. All need the judge's matching, so they come together or not at all."""

    matching: FeatureMatching
    recall: Share
    precision: Share | None  # None unless the label is exhaustive
    partial_matches: int
    live_but_expected_not_live: list[MatchedFeature]  # the costly error
    live_and_not_in_label: list[str]  # for a person to look at
    status_agreement: Share
    status_disagreements: list[MatchedFeature]


class EvidenceScore(_Model):
    claims_with_evidence: Share
    supported: Share  # strict
    at_least_partly_supported: Share
    supported_claims: list[str]
    partly_supported: list[EvidenceOutcome]
    not_supported: list[EvidenceOutcome]
    not_asked: list[str]
    without_usable_verdict: list[str]
    outcomes: list[EvidenceOutcome]


class SectionsScore(_Model):
    presence: dict[str, Presence]
    business_model_type: StatedValue
    trial_days: StatedValue
    attribution: StatedValue
    deep_links: StatedValue
    palette: SetComparison
    analytics: AnalyticsComparison
    judged: dict[str, JudgedSection]


class ProfileScore(_Model):
    """Every metric of one drafted profile. ``features`` is ``None`` without a usable matching."""

    product: ProductScore
    label_is_exhaustive: bool
    expected_statuses: dict[str, int]
    drafted_statuses: dict[str, int]
    feature_matching_state: VerdictState
    features: FeatureScore | None
    evidence: EvidenceScore
    sections: SectionsScore


def _product_score(expected: ProductProfile, drafted: ProductProfile) -> ProductScore:
    return ProductScore(
        expected_name=expected.product.name,
        drafted_name=drafted.product.name,
        name_matches=product_name_matches(expected, drafted),
        expected_type=expected.product.type,
        drafted_type=drafted.product.type,
        type_matches=product_type_matches(expected, drafted),
        platforms=platforms(expected, drafted),
        languages=languages(expected, drafted),
    )


def _feature_score(
    expected: ProductProfile,
    drafted: ProductProfile,
    verdicts: JudgeVerdicts,
    label_is_exhaustive: bool,
) -> FeatureScore | None:
    """Feature metrics from the judge's matching.

    No judge is needed when either list is empty: nothing can match. Otherwise
    a matching that is missing, failed or malformed gives no feature metrics.
    """
    nothing_to_match = not expected.features or not drafted.features
    if verdicts.feature_matching.state is not VerdictState.JUDGED and not nothing_to_match:
        return None
    proposed = [] if nothing_to_match else verdicts.feature_matching.proposed
    matching = match_features(expected, drafted, proposed)
    return FeatureScore(
        matching=matching,
        recall=feature_recall(matching),
        precision=feature_precision(matching, label_is_exhaustive=label_is_exhaustive),
        partial_matches=len(partial_matches(matching)),
        live_but_expected_not_live=live_but_expected_not_live(drafted, matching),
        live_and_not_in_label=live_and_not_in_label(drafted, matching),
        status_agreement=status_agreement(matching),
        status_disagreements=status_disagreements(matching),
    )


def _evidence_score(drafted: ProductProfile, verdicts: JudgeVerdicts) -> EvidenceScore:
    outcomes = evidence_outcomes(drafted, verdicts)
    return EvidenceScore(
        claims_with_evidence=claims_with_evidence(drafted),
        supported=supported_evidence(outcomes),
        at_least_partly_supported=at_least_partly_supported_evidence(outcomes),
        supported_claims=[o.claim for o in evidence_with_support(outcomes, Support.SUPPORTED)],
        partly_supported=evidence_with_support(outcomes, Support.PARTLY_SUPPORTED),
        not_supported=evidence_with_support(outcomes, Support.NOT_SUPPORTED),
        not_asked=evidence_not_asked(outcomes),
        without_usable_verdict=evidence_without_usable_verdict(outcomes),
        outcomes=outcomes,
    )


def score_profile(
    expected: ProductProfile,
    drafted: ProductProfile,
    verdicts: JudgeVerdicts,
    *,
    label_is_exhaustive: bool = False,
) -> ProfileScore:
    """Every metric of ``drafted`` against ``expected``, given the judge's verdicts on the draft.

    ``label_is_exhaustive`` says whether ``expected`` claims to list every
    feature of the product; it decides whether there is a precision.
    """
    return ProfileScore(
        product=_product_score(expected, drafted),
        label_is_exhaustive=label_is_exhaustive,
        expected_statuses=status_counts(expected.features),
        drafted_statuses=status_counts(drafted.features),
        feature_matching_state=verdicts.feature_matching.state,
        features=_feature_score(expected, drafted, verdicts, label_is_exhaustive),
        evidence=_evidence_score(drafted, verdicts),
        sections=SectionsScore(
            presence=section_presence(expected, drafted),
            business_model_type=business_model_type(expected, drafted),
            trial_days=trial_days(expected, drafted),
            attribution=attribution(expected, drafted),
            deep_links=deep_links(expected, drafted),
            palette=palette(expected, drafted),
            analytics=analytics(expected, drafted),
            judged={section: judged_section(verdicts, section) for section in JUDGED_SECTIONS},
        ),
    )
