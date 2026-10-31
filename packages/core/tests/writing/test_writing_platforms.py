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
        "try evil.io",
        "try EVIL.COM",
        "see t.co/abc",
        "see xn--80ak6aa92e.com",
        "see evil.xn--p1ai",
    ],
)
def test_a_link_is_found_with_or_without_a_scheme(text):
    assert links(text)


@pytest.mark.parametrize(
    "text",
    [
        "try evil.top",
        "see evil.click/x",
        "try evil.ninja",
        "try bücher.de",
        "try ex\u0430mple.com",  # a Cyrillic а in the first label
        "try evil\u3002com",  # an ideographic full stop
        "try evil\uff61com",  # its half-width form
        "try evil\uff0ecom",  # a full-width full stop
        "try \uff45\uff56\uff49\uff4c.com",  # full-width letters
        "try ev\u200bil.com",  # a zero-width space inside the label
        "try evil.c\u0301om",  # a combining mark inside the last label
        "try example.\u043a\u043e\u043c",  # a last label in Cyrillic
        "try example.\u4e2d\u56fd",  # a last label in Han
    ],
)
def test_a_bare_domain_is_a_link_under_any_top_level_domain_and_however_it_is_written(text):
    assert links(text)


def test_a_link_is_quoted_as_it_reads_once_normalized():
    assert links("try evil\u3002com now") == ["evil.com"]


@pytest.mark.parametrize(
    "text",
    [
        "e.g. this",
        "i.e. that",
        "version 2.5 is out",
        "v2.0 ships today",
        "It is 3.5 times faster.",
        "Made in the U.S. and loved.",
        "Wait... what? Yes.",
        "Python 3.12 and 1.5x faster",
    ],
)
def test_ordinary_text_with_abbreviations_and_numbers_holds_no_link(text):
    assert links(text) == []


@pytest.mark.parametrize("text", ["Built on Node.js", "It works.Try it"])
def test_a_word_joined_to_another_by_a_dot_is_refused_as_a_link(text):
    # Telling a product name from a domain would take a list of top-level domains again.
    assert links(text)
