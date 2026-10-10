"""Tests for the metrics: hand-made pairs of profiles and hand-made verdicts, no model."""

import pytest

from openmarketer_evaluation import scoring
from openmarketer_evaluation.verdicts import (
    Agreement,
    EvidenceVerdict,
    FeatureMatchingVerdict,
    JudgeVerdicts,
    MatchKind,
    ProposedMatch,
    SectionVerdict,
    Support,
    VerdictState,
)


def pair(expected_id: str, drafted_id: str, kind: MatchKind = MatchKind.SAME) -> ProposedMatch:
    return ProposedMatch(expected_id=expected_id, drafted_id=drafted_id, kind=kind)


def verdicts(*proposed: ProposedMatch, state=VerdictState.JUDGED, evidence=(), sections=()):
    return JudgeVerdicts(
        case="notes",
        run=1,
        feature_matching=FeatureMatchingVerdict(state=state, proposed=list(proposed)),
        evidence=list(evidence),
        sections=list(sections),
    )


def supported(
    claim: str, support: Support = Support.SUPPORTED, reason: str = ""
) -> EvidenceVerdict:
    return EvidenceVerdict(claim=claim, state=VerdictState.JUDGED, support=support, reason=reason)


# ------------------------------------------------------------------ product
@pytest.mark.parametrize(
    ("drafted", "matches"),
    [
        ("Notes", True),
        ("  notes ", True),
        ("NOTES", True),
        ("Notes  App", False),
        ("Notes: shared memories", False),
    ],
)
def test_product_name_matches_after_case_and_whitespace_only(profile, drafted, matches):
    assert scoring.product_name_matches(profile(), profile(product={"name": drafted})) is matches


def test_name_with_several_spaces_is_one_name(profile):
    expected = profile(product={"name": "Notes: Shared Notebooks"})
    drafted = profile(product={"name": "notes:  shared NOTEBOOKS"})
    assert scoring.product_name_matches(expected, drafted)


def test_product_type_is_compared_as_a_slug(profile):
    assert scoring.product_type_matches(profile(), profile())
    assert not scoring.product_type_matches(profile(), profile(product={"type": "dev_tool"}))


def test_platforms_are_compared_as_sets(profile):
    drafted = profile(product={"platforms": ["ios", "watchos", "ios"]})
    compared = scoring.platforms(profile(), drafted)
    assert (compared.missed, compared.extra, compared.exact) == (["android"], ["watchos"], False)


def test_platforms_in_another_order_are_exact(profile):
    assert scoring.platforms(profile(), profile(product={"platforms": ["ios", "android"]})).exact


# ----------------------------------------------------------------- features
def test_matched_missed_and_not_in_the_label(profile, feature):
    expected = profile(feature("share-memories"), feature("pair-devices", "unreleased"))
    drafted = profile(feature("sharing"), feature("dark-mode"))
    matching = scoring.match_features(expected, drafted, [pair("share-memories", "sharing")])
    assert [(m.expected_id, m.drafted_id) for m in matching.matched] == [
        ("share-memories", "sharing")
    ]
    assert [(m.id, m.status.value) for m in matching.missed] == [("pair-devices", "unreleased")]
    assert matching.not_in_label == ["dark-mode"]
    assert scoring.feature_recall(matching).ratio == 0.5


def test_feature_is_matched_once_and_the_first_pair_keeps_it(profile, feature):
    expected = profile(feature("share-memories"), feature("pair-devices"))
    drafted = profile(feature("sharing"))
    proposed = [pair("share-memories", "sharing"), pair("pair-devices", "sharing")]
    matching = scoring.match_features(expected, drafted, proposed)
    assert [m.expected_id for m in matching.matched] == ["share-memories"]
    assert matching.discarded == [proposed[1]]
    assert [m.id for m in matching.missed] == ["pair-devices"]


def test_same_is_taken_before_partial_whatever_the_order(profile, feature):
    expected = profile(feature("share-memories"), feature("pair-devices"))
    drafted = profile(feature("sharing"))
    partial = pair("pair-devices", "sharing", MatchKind.PARTIAL)
    matching = scoring.match_features(
        expected, drafted, [partial, pair("share-memories", "sharing")]
    )
    assert [(m.expected_id, m.kind) for m in matching.matched] == [
        ("share-memories", MatchKind.SAME)
    ]
    assert matching.discarded == [partial]


def test_partial_match_counts_as_matched_and_is_listed_apart(profile, feature):
    expected, drafted = profile(feature("share-memories")), profile(feature("sharing"))
    matching = scoring.match_features(
        expected, drafted, [pair("share-memories", "sharing", MatchKind.PARTIAL)]
    )
    assert (matching.missed, matching.not_in_label) == ([], [])
    assert [m.drafted_id for m in scoring.partial_matches(matching)] == ["sharing"]


def test_pair_naming_a_feature_neither_profile_has_is_discarded(profile, feature):
    expected, drafted = profile(feature("share-memories")), profile(feature("sharing"))
    made_up = pair("share-memories", "no-such-feature")
    matching = scoring.match_features(expected, drafted, [made_up])
    assert (matching.matched, matching.discarded) == ([], [made_up])
    assert [m.id for m in matching.missed] == ["share-memories"]
    assert matching.not_in_label == ["sharing"]


def test_nothing_drafted_misses_everything(profile, feature):
    expected = profile(feature("share-memories"), feature("pair-devices"))
    matching = scoring.match_features(expected, profile(), [])
    assert [m.id for m in matching.missed] == ["share-memories", "pair-devices"]
    assert scoring.feature_recall(matching).ratio == 0.0
    precision = scoring.feature_precision(matching, label_is_exhaustive=True)
    assert precision is not None and precision.ratio is None


def test_nothing_expected_leaves_every_drafted_feature_outside_the_label(profile, feature):
    drafted = profile(feature("sharing"), feature("dark-mode"))
    matching = scoring.match_features(profile(), drafted, [])
    assert matching.not_in_label == ["sharing", "dark-mode"]
    assert scoring.feature_recall(matching).ratio is None


# ------------------------------------------- precision and the label's reach
def test_there_is_no_precision_against_a_label_that_is_not_exhaustive(profile, feature):
    expected, drafted = profile(feature("share-memories")), profile(feature("dark-mode"))
    matching = scoring.match_features(expected, drafted, [])
    assert scoring.feature_precision(matching, label_is_exhaustive=False) is None


def test_precision_against_an_exhaustive_label_counts_what_it_lacks_as_invented(profile, feature):
    expected = profile(feature("share-memories"))
    drafted = profile(feature("sharing"), feature("dark-mode"))
    matching = scoring.match_features(expected, drafted, [pair("share-memories", "sharing")])
    precision = scoring.feature_precision(matching, label_is_exhaustive=True)
    assert precision is not None and (precision.count, precision.of) == (1, 2)


def test_profile_score_has_a_precision_only_for_an_exhaustive_label(profile, feature):
    expected, drafted = profile(feature("share-memories")), profile(feature("sharing"))
    judged = verdicts(pair("share-memories", "sharing"))
    open_label = scoring.score_profile(expected, drafted, judged)
    closed_label = scoring.score_profile(expected, drafted, judged, label_is_exhaustive=True)
    assert open_label.features is not None and open_label.features.precision is None
    assert not open_label.label_is_exhaustive
    assert closed_label.features is not None and closed_label.features.precision is not None
    assert closed_label.features.precision.ratio == 1.0


# -------------------------------------- the costly error, and what is not one
def test_feature_live_in_the_draft_and_unreleased_in_the_label_is_the_costly_error(
    profile, feature
):
    expected = profile(feature("share-memories", "unreleased"))
    drafted = profile(feature("sharing", "live"))
    matching = scoring.match_features(expected, drafted, [pair("share-memories", "sharing")])
    (costly,) = scoring.live_but_expected_not_live(drafted, matching)
    assert (costly.drafted_id, costly.expected_id, costly.expected_status.value) == (
        "sharing",
        "share-memories",
        "unreleased",
    )
    assert scoring.live_and_not_in_label(drafted, matching) == []


def test_feature_live_in_the_draft_and_unknown_in_the_label_is_the_costly_error(profile, feature):
    expected = profile(feature("share-memories", "unknown"))
    drafted = profile(feature("sharing"))
    matching = scoring.match_features(expected, drafted, [pair("share-memories", "sharing")])
    (costly,) = scoring.live_but_expected_not_live(drafted, matching)
    assert costly.expected_status.value == "unknown"


def test_live_feature_the_label_does_not_have_is_not_the_costly_error(profile, feature):
    drafted = profile(feature("dark-mode"))
    matching = scoring.match_features(profile(feature("share-memories")), drafted, [])
    assert scoring.live_but_expected_not_live(drafted, matching) == []
    assert scoring.live_and_not_in_label(drafted, matching) == ["dark-mode"]


def test_feature_outside_the_label_that_is_not_live_is_in_neither_list(profile, feature):
    drafted = profile(feature("dark-mode", "unreleased"), feature("themes", "unknown"))
    matching = scoring.match_features(profile(), drafted, [])
    assert scoring.live_but_expected_not_live(drafted, matching) == []
    assert scoring.live_and_not_in_label(drafted, matching) == []
    assert matching.not_in_label == ["dark-mode", "themes"]


def test_feature_live_on_both_sides_is_in_neither_list(profile, feature):
    expected, drafted = profile(feature("share-memories")), profile(feature("sharing"))
    matching = scoring.match_features(expected, drafted, [pair("share-memories", "sharing")])
    assert scoring.live_but_expected_not_live(drafted, matching) == []
    assert scoring.live_and_not_in_label(drafted, matching) == []


def test_draft_more_careful_than_the_label_is_a_status_disagreement_and_no_costly_error(
    profile, feature
):
    expected, drafted = profile(feature("share-memories")), profile(feature("sharing", "unknown"))
    matching = scoring.match_features(expected, drafted, [pair("share-memories", "sharing")])
    assert scoring.live_but_expected_not_live(drafted, matching) == []
    assert scoring.status_agreement(matching).ratio == 0.0
    assert [m.drafted_id for m in scoring.status_disagreements(matching)] == ["sharing"]


def test_profile_score_keeps_the_two_kinds_of_live_feature_apart(profile, feature):
    expected = profile(feature("share-memories"), feature("pair-devices", "unreleased"))
    drafted = profile(feature("sharing"), feature("pairing"), feature("dark-mode"))
    judged = verdicts(pair("share-memories", "sharing"), pair("pair-devices", "pairing"))
    score = scoring.score_profile(expected, drafted, judged)
    assert score.features is not None
    assert [m.drafted_id for m in score.features.live_but_expected_not_live] == ["pairing"]
    assert score.features.live_and_not_in_label == ["dark-mode"]
    assert score.features.recall.ratio == 1.0


def test_status_agreement_counts_matched_pairs_only(profile, feature):
    expected = profile(feature("share-memories"), feature("pair-devices", "unreleased"))
    drafted = profile(feature("sharing"), feature("pairing"), feature("dark-mode", "unknown"))
    matching = scoring.match_features(
        expected, drafted, [pair("share-memories", "sharing"), pair("pair-devices", "pairing")]
    )
    agreement = scoring.status_agreement(matching)
    assert (agreement.count, agreement.of) == (1, 2)


def test_status_agreement_of_no_matched_pair_is_not_a_number(profile, feature):
    matching = scoring.match_features(profile(), profile(feature("sharing")), [])
    assert scoring.status_agreement(matching).ratio is None


def test_statuses_of_both_feature_lists_are_counted_with_every_status_named(profile, feature):
    expected = profile(feature("share-memories"), feature("pair-devices", "unreleased"))
    drafted = profile(feature("sharing"), feature("pairing"))
    score = scoring.score_profile(expected, drafted, verdicts())
    assert score.expected_statuses == {"live": 1, "unreleased": 1, "unknown": 0}
    assert score.drafted_statuses == {"live": 2, "unreleased": 0, "unknown": 0}


# ----------------------------------------------------------------- evidence
def test_claims_with_evidence_counts_product_filled_sections_and_features(profile, feature):
    drafted = profile(
        feature("sharing"),
        feature("pairing", evidence=[]),
        audience={"primary": "families", "confidence": 0.4},
    )
    cited = scoring.claims_with_evidence(drafted)
    assert (cited.count, cited.of) == (2, 4)  # product and one feature, of four claims


def test_section_that_says_nothing_is_not_a_claim_even_with_evidence(profile):
    drafted = profile(measurement={"evidence": [{"file": "README.md"}], "confidence": 0.8})
    assert scoring.claims_with_evidence(drafted).of == 1


def test_evidence_has_three_outcomes_reported_apart(profile, feature):
    drafted = profile(feature("sharing"), feature("pairing"))
    judged = verdicts(
        evidence=[
            supported("product"),
            supported("feature:sharing", Support.PARTLY_SUPPORTED, "only the imports are shown"),
            supported("feature:pairing", Support.NOT_SUPPORTED, "about something else"),
        ]
    )
    evidence = scoring.score_profile(profile(), drafted, judged).evidence
    assert evidence.supported_claims == ["product"]
    assert [(o.claim, o.reason) for o in evidence.partly_supported] == [
        ("feature:sharing", "only the imports are shown")
    ]
    assert [(o.claim, o.reason) for o in evidence.not_supported] == [
        ("feature:pairing", "about something else")
    ]


def test_strict_share_counts_supported_and_the_other_share_adds_partly(profile, feature):
    drafted = profile(feature("sharing"), feature("pairing"))
    judged = verdicts(
        evidence=[
            supported("product"),
            supported("feature:sharing", Support.PARTLY_SUPPORTED),
            supported("feature:pairing", Support.NOT_SUPPORTED),
        ]
    )
    outcomes = scoring.evidence_outcomes(drafted, judged)
    strict = scoring.supported_evidence(outcomes)
    lenient = scoring.at_least_partly_supported_evidence(outcomes)
    assert (strict.count, strict.of) == (1, 3)
    assert (lenient.count, lenient.of) == (2, 3)


def test_malformed_evidence_verdict_is_left_out_of_the_shares_and_named(profile, feature):
    drafted = profile(feature("sharing"))
    judged = verdicts(
        evidence=[
            supported("product"),
            EvidenceVerdict(claim="feature:sharing", state=VerdictState.MALFORMED, reply="yes!"),
        ]
    )
    outcomes = scoring.evidence_outcomes(drafted, judged)
    share = scoring.supported_evidence(outcomes)
    assert (share.count, share.of) == (1, 1)
    assert scoring.evidence_without_usable_verdict(outcomes) == ["feature:sharing"]
    assert scoring.evidence_not_asked(outcomes) == []


def test_claim_the_judge_was_not_asked_about_is_told_apart_from_a_bad_verdict(profile, feature):
    drafted = profile(feature("sharing"), feature("pairing"))
    judged = verdicts(
        evidence=[
            supported("product"),
            EvidenceVerdict(claim="feature:sharing", state=VerdictState.NOT_ASKED),
            EvidenceVerdict(claim="feature:pairing", state=VerdictState.FAILED),
        ]
    )
    outcomes = scoring.evidence_outcomes(drafted, judged)
    assert scoring.evidence_not_asked(outcomes) == ["feature:sharing"]
    assert scoring.evidence_without_usable_verdict(outcomes) == ["feature:pairing"]


def test_claim_without_evidence_is_neither_judged_nor_missing_a_verdict(profile, feature):
    drafted = profile(feature("sharing", evidence=[]))
    outcomes = scoring.evidence_outcomes(drafted, verdicts(evidence=[supported("product")]))
    assert scoring.evidence_not_asked(outcomes) == []
    assert scoring.supported_evidence(outcomes).of == 1


def test_shares_of_nothing_judged_are_not_numbers(profile):
    outcomes = scoring.evidence_outcomes(profile(), verdicts())
    assert scoring.supported_evidence(outcomes).ratio is None
    assert scoring.at_least_partly_supported_evidence(outcomes).ratio is None


# ----------------------------------------------------------- other sections
def test_section_presence_tells_filled_from_empty_on_both_sides(profile):
    expected = profile(audience={"primary": "families"}, business_model={"type": "free"})
    drafted = profile(audience={"primary": "friends"}, brand={"voice": "warm"})
    assert scoring.section_presence(expected, drafted) == {
        "brand": scoring.Presence.FILLED_WHERE_EXPECTED_EMPTY,
        "audience": scoring.Presence.BOTH_FILLED,
        "business_model": scoring.Presence.EMPTY_WHERE_EXPECTED_FILLED,
        "measurement": scoring.Presence.BOTH_EMPTY,
    }


def test_business_model_type_is_compared_exactly(profile):
    free = profile(business_model={"type": "free"})
    assert scoring.business_model_type(free, free).same
    compared = scoring.business_model_type(free, profile())
    assert (compared.expected, compared.drafted, compared.same) == ("free", "unknown", False)


def test_trial_the_label_does_not_state_and_the_draft_does_differs(profile):
    drafted = profile(business_model={"type": "free", "trial_days": 14})
    compared = scoring.trial_days(profile(business_model={"type": "free"}), drafted)
    assert (compared.expected, compared.drafted, compared.same) == (None, 14, False)


def test_attribution_stated_only_by_the_draft_differs(profile):
    drafted = profile(measurement={"attribution": "Firebase for storage"})
    compared = scoring.attribution(profile(), drafted)
    assert (compared.expected, compared.drafted, compared.same) == (
        None,
        "Firebase for storage",
        False,
    )
    assert scoring.attribution(drafted, drafted).same


def test_deep_links_not_stated_and_no_are_different_answers(profile):
    compared = scoring.deep_links(profile(), profile(measurement={"deep_links": False}))
    assert (compared.expected, compared.drafted, compared.same) == (None, False, False)


def test_palette_is_compared_as_a_set_whatever_the_case(profile):
    expected = profile(brand={"palette": ["#006275", "#ee9b00"]})
    drafted = profile(brand={"palette": ["#EE9B00", "#FFFFFF"]})
    compared = scoring.palette(expected, drafted)
    assert (compared.missed, compared.extra) == (["#006275"], ["#FFFFFF"])


def analytics_of(profile, expected: list[str], drafted: list[str]):
    return scoring.analytics(
        profile(measurement={"analytics": expected}), profile(measurement={"analytics": drafted})
    )


def test_analytics_tool_named_as_written_is_found(profile):
    compared = analytics_of(profile, ["Firebase Analytics"], ["firebase  analytics", "Mixpanel"])
    assert (compared.found, compared.missed) == (["firebase analytics"], [])
    assert (compared.extra, compared.not_names) == (["Mixpanel"], [])


def test_analytics_tool_buried_in_a_sentence_is_found_and_the_sentence_is_reported(profile):
    sentence = "Simple analytics via window.sa_event (Sentry Analytics), gated by a flag"
    compared = analytics_of(profile, ["Simple Analytics"], [sentence])
    assert (compared.found, compared.missed, compared.extra) == (["simple analytics"], [], [])
    assert compared.not_names == [sentence]


def test_analytics_tool_is_not_found_inside_a_longer_word(profile):
    compared = analytics_of(profile, ["Segment"], ["Segmentation by cohort"])
    assert (compared.found, compared.missed) == ([], ["segment"])
    assert compared.extra == ["Segmentation by cohort"]


def test_analytics_tool_the_draft_does_not_name_is_missed(profile):
    compared = analytics_of(profile, ["Simple Analytics", "Mixpanel"], ["Mixpanel"])
    assert (compared.found, compared.missed) == (["mixpanel"], ["simple analytics"])


@pytest.mark.parametrize(
    "entry",
    [
        "Tracking through an event bus with a whitelist",
        "Firebase (Firestore)",
        "Mixpanel, Amplitude",
        "Analytics: none found",
        "A" * 41,
    ],
)
def test_analytics_entry_that_is_not_a_short_name_is_a_defect_of_the_draft(profile, entry):
    drafted = profile(measurement={"analytics": [entry]})
    assert scoring.analytics_entries_that_are_not_names(drafted) == [entry]


@pytest.mark.parametrize("entry", ["Mixpanel", "Google Analytics 4", "Simple Analytics"])
def test_short_tool_name_is_no_defect(profile, entry):
    drafted = profile(measurement={"analytics": [entry]})
    assert scoring.analytics_entries_that_are_not_names(drafted) == []


def test_judged_section_carries_the_verdict_and_its_reason_or_says_why_not(profile):
    judged = verdicts(
        sections=[
            SectionVerdict(
                section="brand_voice",
                state=VerdictState.JUDGED,
                agreement=Agreement.PARTLY_AGREES,
                reason="one is warmer",
            ),
            SectionVerdict(section="audience", state=VerdictState.MALFORMED, reply="maybe"),
        ]
    )
    voice = scoring.judged_section(judged, "brand_voice")
    assert (voice.agreement, voice.reason) == (Agreement.PARTLY_AGREES, "one is warmer")
    audience = scoring.judged_section(judged, "audience")
    assert (audience.state, audience.agreement) == (VerdictState.MALFORMED, None)
    assert scoring.judged_section(verdicts(), "audience").state is VerdictState.NOT_ASKED


# ---------------------------------------------------------------- a profile
def test_profile_score_without_a_usable_matching_has_no_feature_metrics(profile, feature):
    expected, drafted = profile(feature("share-memories")), profile(feature("sharing"))
    for state in (VerdictState.MALFORMED, VerdictState.FAILED, VerdictState.NOT_ASKED):
        score = scoring.score_profile(expected, drafted, verdicts(state=state))
        assert score.features is None
        assert score.feature_matching_state is state
        assert score.product.name_matches


def test_profile_score_needs_no_judge_when_nothing_was_drafted(profile, feature):
    expected = profile(feature("share-memories"))
    score = scoring.score_profile(expected, profile(), verdicts(state=VerdictState.NOT_ASKED))
    assert score.features is not None
    assert [m.id for m in score.features.matching.missed] == ["share-memories"]
    assert score.features.live_but_expected_not_live == []


def test_profile_score_survives_being_written_and_read(profile, feature):
    expected, drafted = profile(feature("share-memories")), profile(feature("sharing"))
    score = scoring.score_profile(expected, drafted, verdicts(pair("share-memories", "sharing")))
    assert scoring.ProfileScore.model_validate_json(score.model_dump_json()) == score
