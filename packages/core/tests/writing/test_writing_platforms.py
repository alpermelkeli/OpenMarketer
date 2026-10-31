"""Tests for what a platform asks of a post's text: X's length, hashtags, links, mentions."""

import pytest

from openmarketer_core.writing.brief import WritingPlatform
from openmarketer_core.writing.platforms import (
    hashtags,
    links,
    mentions,
    platform_rules,
    x_weighted_length,
)


def test_latin_text_counts_one_per_character():
    assert x_weighted_length("Ship it today.") == 14


def test_cjk_characters_count_two():
    assert x_weighted_length("今日は") == 6


def test_the_punctuation_ranges_of_weight_one_count_one():
    # an en dash, curly quotes and a prime are in X's ranges of weight 1
    assert x_weighted_length("–“”′") == 4


def test_an_emoji_counts_two():
    assert x_weighted_length("🚀") == 2


@pytest.mark.parametrize(
    "emoji",
    [
        "👩‍💻",  # a sequence with a zero-width joiner
        "👍🏽",  # a skin tone
        "🇹🇷",  # a flag
        "1️⃣",  # a keycap
        "❤️",  # with the emoji presentation
        "👨‍👩‍👧‍👦",  # a family of four joined
    ],
)
def test_an_emoji_made_of_several_code_points_counts_two(emoji):
    assert x_weighted_length(emoji) == 2


def test_a_digit_without_a_keycap_counts_one():
    assert x_weighted_length("1") == 1


def test_the_text_is_counted_after_nfc_normalization():
    decomposed = "é"  # e and a combining acute accent: é once composed
    assert x_weighted_length(decomposed) == 1


def test_x_allows_280_and_two_hashtags_and_no_links_or_mentions():
    rules = platform_rules(WritingPlatform.X)
    assert rules.max_length == 280
    assert rules.max_hashtags == 2
    assert not rules.allows_links and not rules.allows_mentions


def test_hashtags_need_a_letter_and_a_boundary():
    assert hashtags("#launch day, #1 again, and#not, #día") == ["#launch", "#día"]


def test_mentions_are_found_but_an_email_is_not_one():
    assert mentions("thanks @acme_app and hi@example.org") == ["@acme_app"]


@pytest.mark.parametrize(
    "text",
    [
        "see https://example.com/x",
        "see http://example.com",
        "go to www.example.org",
        "try acme.app today",
        "mail hi@example.io",
    ],
)
def test_a_link_is_found_with_or_without_a_scheme(text):
    assert links(text)


@pytest.mark.parametrize("text", ["Built on Node.js", "e.g. this", "version 2.5 is out"])
def test_words_with_dots_that_are_not_domains_are_not_links(text):
    assert links(text) == []
