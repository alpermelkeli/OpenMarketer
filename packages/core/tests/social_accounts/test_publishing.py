"""Tests for the publishing port's plain rules: what a retried attempt does, how texts compare."""

from datetime import UTC, datetime

import pytest

from openmarketer_core.social_accounts.publishing import (
    LOOKUP_MARGIN,
    PostImageFile,
    PublishedPost,
    PublishingFailure,
    PublishingRefused,
    PublishingSituation,
    PublishingStage,
    PublishingStep,
    RefusalReason,
    lookup_since,
    next_publishing_step,
    text_fingerprint,
    text_key,
)

FOUND = PublishedPost(external_id="1850", url="https://x.com/acme/status/1850")


def step(stage: PublishingStage, **known) -> PublishingStep:
    return next_publishing_step(PublishingSituation(stage=stage, **known))


def test_a_first_attempt_sends_without_looking_up():
    assert step(PublishingStage.STARTED) is PublishingStep.SEND


def test_a_post_already_published_is_not_sent_again():
    assert step(PublishingStage.DONE) is PublishingStep.NOTHING
    assert step(PublishingStage.DONE, refused_as_duplicate=True) is PublishingStep.NOTHING


def test_a_send_whose_outcome_is_unknown_is_looked_up_before_anything_is_sent():
    assert step(PublishingStage.RESUMED) is PublishingStep.LOOK_UP


def test_a_send_whose_post_is_found_records_it_instead_of_sending_again():
    assert step(PublishingStage.RESUMED, looked_up=True, found=FOUND) is PublishingStep.RECORD_FOUND


def test_a_send_whose_post_is_not_found_is_sent_again():
    assert step(PublishingStage.RESUMED, looked_up=True) is PublishingStep.SEND


@pytest.mark.parametrize("stage", [PublishingStage.STARTED, PublishingStage.RESUMED])
def test_a_send_refused_as_a_duplicate_is_looked_up(stage):
    assert step(stage, refused_as_duplicate=True) is PublishingStep.LOOK_UP


@pytest.mark.parametrize("stage", [PublishingStage.STARTED, PublishingStage.RESUMED])
def test_a_duplicate_whose_post_is_found_is_recorded_as_published(stage):
    found = step(stage, refused_as_duplicate=True, looked_up=True, found=FOUND)

    assert found is PublishingStep.RECORD_FOUND


@pytest.mark.parametrize("stage", [PublishingStage.STARTED, PublishingStage.RESUMED])
def test_a_duplicate_whose_post_is_not_found_fails_and_is_never_sent_again(stage):
    assert step(stage, refused_as_duplicate=True, looked_up=True) is PublishingStep.FAIL_DUPLICATE


def test_a_resumed_send_is_never_sent_twice_in_one_attempt():
    # Not found, sent, refused as a duplicate: the situation starts again from the refusal.
    steps = [step(PublishingStage.RESUMED)]
    steps.append(step(PublishingStage.RESUMED, looked_up=True))
    steps.append(step(PublishingStage.RESUMED, refused_as_duplicate=True))
    steps.append(step(PublishingStage.RESUMED, refused_as_duplicate=True, looked_up=True))

    assert steps == [
        PublishingStep.LOOK_UP,
        PublishingStep.SEND,
        PublishingStep.LOOK_UP,
        PublishingStep.FAIL_DUPLICATE,
    ]


def test_a_lookup_reads_from_a_little_before_the_send_started():
    started = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)

    assert lookup_since(started) == started - LOOKUP_MARGIN


def test_two_texts_differing_in_whitespace_and_unicode_form_are_the_same_post():
    composed = "Café notes,\n  offline too. "
    decomposed = "Café notes, offline   too."

    assert text_key(composed) == text_key(decomposed) == "Café notes, offline too."
    assert text_fingerprint(composed) == text_fingerprint(decomposed)


def test_case_and_punctuation_make_another_post():
    assert text_fingerprint("Notes, offline.") != text_fingerprint("notes, offline!")


def test_a_refusal_says_its_category_and_whether_it_is_a_duplicate():
    duplicate = PublishingRefused("X", RefusalReason.DUPLICATE)
    too_long = PublishingRefused("X", RefusalReason.TOO_LONG)

    assert duplicate.duplicate and not too_long.duplicate
    assert str(too_long) == "X refused the post: the platform says the text is too long"
    assert PublishingFailure.of_refusal(RefusalReason.MEDIA_REFUSED) is (
        PublishingFailure.MEDIA_REFUSED
    )


def test_every_refusal_can_be_recorded_as_a_failure():
    assert {PublishingFailure.of_refusal(reason) for reason in RefusalReason} | {
        PublishingFailure.ACCESS_REVOKED
    } == set(PublishingFailure)


def test_an_images_bytes_are_not_in_its_repr():
    assert "secret-bytes" not in repr(PostImageFile(data=b"secret-bytes", media_type="image/png"))
