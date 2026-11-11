"""Tests for OAuth 1.0a signing, against the example of X's "Creating a signature" page."""

import pytest

from openmarketer_core.social_accounts.oauth1 import (
    OAuth1Client,
    OAuth1Token,
    authorization_header,
    base_string_uri,
    percent_encode,
    signature,
    signature_base_string,
    signing_key,
)

# X's published example: https://developer.x.com/en/docs/authentication/oauth-1-0a/creating-a-signature
CLIENT = OAuth1Client(
    key="xvz1evFS4wEEPTGEFPHBog", secret="kAcSOqF21Fu85e7zjz7ZN2U4ZRhfV3WpwPAoE3Z7kBw"
)
TOKEN = OAuth1Token(
    token="370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb",
    secret="LswwdoUaIvS8ltyTt5jkRh4J50vUPVVHtR2YPi5kE",
)
URL = "https://api.twitter.com/1.1/statuses/update.json?include_entities=true"
STATUS = [("status", "Hello Ladies + Gentlemen, a signed OAuth request!")]
NONCE = "kYjzVBB8Y0ZFabxSWbWovY3uYSQ2pTgmZeNu2VS4cg"
TIMESTAMP = 1318622958
OAUTH = [
    ("oauth_consumer_key", CLIENT.key),
    ("oauth_nonce", NONCE),
    ("oauth_signature_method", "HMAC-SHA1"),
    ("oauth_timestamp", str(TIMESTAMP)),
    ("oauth_token", TOKEN.token),
    ("oauth_version", "1.0"),
]
BASE_STRING = (
    "POST&https%3A%2F%2Fapi.twitter.com%2F1.1%2Fstatuses%2Fupdate.json&include_entities%3Dtrue"
    "%26oauth_consumer_key%3Dxvz1evFS4wEEPTGEFPHBog%26oauth_nonce%3DkYjzVBB8Y0ZFabxSWbWovY3uYSQ2"
    "pTgmZeNu2VS4cg%26oauth_signature_method%3DHMAC-SHA1%26oauth_timestamp%3D1318622958%26oauth_"
    "token%3D370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb%26oauth_version%3D1.0%26status%3D"
    "Hello%2520Ladies%2520%252B%2520Gentlemen%252C%2520a%2520signed%2520OAuth%2520request%2521"
)
SIGNING_KEY = (
    "kAcSOqF21Fu85e7zjz7ZN2U4ZRhfV3WpwPAoE3Z7kBw&"  # the consumer secret, then the token's
    "LswwdoUaIvS8ltyTt5jkRh4J50vUPVVHtR2YPi5kE"
)
SIGNATURE = "hCtSmYh+iHYCEqBWrE7C7hYmtUk="


def test_base_string_is_the_one_x_publishes():
    assert signature_base_string("POST", URL, [*OAUTH, *STATUS]) == BASE_STRING


def test_signing_key_joins_both_secrets():
    assert signing_key(CLIENT, TOKEN) == SIGNING_KEY


def test_signature_is_the_one_x_publishes():
    assert signature(BASE_STRING, SIGNING_KEY) == SIGNATURE


def test_header_carries_the_published_signature_and_every_oauth_parameter():
    header = authorization_header(
        "POST", URL, client=CLIENT, token=TOKEN, nonce=NONCE, timestamp=TIMESTAMP, parameters=STATUS
    )
    assert header == (
        'OAuth oauth_consumer_key="xvz1evFS4wEEPTGEFPHBog", '
        'oauth_nonce="kYjzVBB8Y0ZFabxSWbWovY3uYSQ2pTgmZeNu2VS4cg", '
        'oauth_signature="hCtSmYh%2BiHYCEqBWrE7C7hYmtUk%3D", '
        'oauth_signature_method="HMAC-SHA1", '
        'oauth_timestamp="1318622958", '
        'oauth_token="370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb", '
        'oauth_version="1.0"'
    )


def test_without_a_token_the_key_ends_with_an_empty_secret_and_no_token_is_sent():
    header = authorization_header(
        "POST", URL, client=CLIENT, token=None, nonce=NONCE, timestamp=TIMESTAMP
    )
    assert signing_key(CLIENT, None) == "kAcSOqF21Fu85e7zjz7ZN2U4ZRhfV3WpwPAoE3Z7kBw&"
    assert "oauth_token" not in header


def test_a_step_s_own_oauth_parameters_are_signed_and_sent():
    header = authorization_header(
        "POST",
        "https://api.x.com/oauth/request_token",
        client=CLIENT,
        token=None,
        nonce=NONCE,
        timestamp=TIMESTAMP,
        oauth_parameters={"oauth_callback": "http://localhost:8000/callback?a=1"},
    )
    assert 'oauth_callback="http%3A%2F%2Flocalhost%3A8000%2Fcallback%3Fa%3D1"' in header


@pytest.mark.parametrize(
    ("text", "encoded"),
    [
        ("Ladies + Gentlemen", "Ladies%20%2B%20Gentlemen"),
        ("An encoded string!", "An%20encoded%20string%21"),
        ("Dogs, Cats & Mice", "Dogs%2C%20Cats%20%26%20Mice"),
        ("-._~", "-._~"),
        ("☃", "%E2%98%83"),
    ],
)
def test_percent_encoding_keeps_only_the_unreserved_characters(text, encoded):
    assert percent_encode(text) == encoded


@pytest.mark.parametrize(
    ("url", "uri"),
    [
        ("HTTPS://API.X.com/2/users/me?user.fields=name#top", "https://api.x.com/2/users/me"),
        ("https://api.x.com:443/oauth/access_token", "https://api.x.com/oauth/access_token"),
        ("http://example.com:8080/r%20v/X?id=123", "http://example.com:8080/r%20v/X"),
    ],
)
def test_base_string_uri_drops_the_query_the_fragment_and_a_default_port(url, uri):
    assert base_string_uri(url) == uri


def test_secrets_are_not_in_repr():
    assert CLIENT.secret not in repr(CLIENT)
    assert TOKEN.secret not in repr(TOKEN)
    assert TOKEN.token not in repr(TOKEN)
