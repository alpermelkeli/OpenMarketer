"""Tests for X's OAuth 1.0a adapter, over a fake transport: nothing reaches X."""

import logging
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from openmarketer_core.social_accounts.catalog import AccountDescription
from openmarketer_core.social_accounts.connection import (
    AuthorizationRefused,
    AuthorizationUnavailable,
    PlatformApp,
)
from openmarketer_core.social_accounts.oauth1 import (
    OAuth1Client,
    OAuth1Token,
    authorization_header,
)
from openmarketer_core.social_accounts.x.authorization import (
    ACCESS_TOKEN_URL,
    AUTHENTICATE_URL,
    REQUEST_TOKEN_URL,
    USERS_ME_URL,
    XAuthorization,
)

APP = PlatformApp(key="app-key", secret="app-secret")
CALLBACK = "http://localhost:8000/v1/social/x/callback"
NONCE = "fixed-nonce"
TIMESTAMP = 1700000000
REQUEST_TOKEN = "request-token"
REQUEST_SECRET = "request-secret"
ACCESS_TOKEN = "42-access-token"
ACCESS_SECRET = "access-secret"
VERIFIER = "the-verifier"
USER = {
    "id": "42",
    "username": "acme",
    "name": "Acme Inc.",
    "profile_image_url": "https://pbs.twimg.com/profile_images/1/acme_normal.png",
}


def x_answers(routes):
    """A transport that answers each URL (without its query) from ``routes`` and keeps requests."""
    sent: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        url = str(request.url).split("?")[0]
        return routes[url](request)

    return httpx.MockTransport(handle), sent


def form(**fields):
    return lambda request: httpx.Response(200, text="&".join(f"{k}={v}" for k, v in fields.items()))


def users_me(data=USER):
    return lambda request: httpx.Response(200, json={"data": data})


def answering(status, text=""):
    return lambda request: httpx.Response(status, text=text)


def authorization(transport):
    return XAuthorization(transport=transport, nonce=lambda: NONCE, clock=lambda: TIMESTAMP)


def oauth_fields(request: httpx.Request) -> dict[str, str]:
    header = request.headers["Authorization"]
    assert header.startswith("OAuth ")
    pairs = (item.split("=", 1) for item in header[len("OAuth ") :].split(", "))
    return {name: value.strip('"') for name, value in pairs}


FINISHED = {
    ACCESS_TOKEN_URL: form(
        oauth_token=ACCESS_TOKEN, oauth_token_secret=ACCESS_SECRET, user_id="42", screen_name="acme"
    ),
    USERS_ME_URL: users_me(),
}


def finish(routes=FINISHED):
    transport, sent = x_answers(routes)
    account = authorization(transport).finish(
        APP, state=REQUEST_TOKEN, grant=VERIFIER, kept_secret=REQUEST_SECRET
    )
    return account, sent


# ------------------------------------------------------------------ start
def test_start_answers_x_s_page_the_request_token_and_its_secret():
    transport, _ = x_answers(
        {
            REQUEST_TOKEN_URL: form(
                oauth_token=REQUEST_TOKEN,
                oauth_token_secret=REQUEST_SECRET,
                oauth_callback_confirmed="true",
            )
        }
    )
    started = authorization(transport).start(APP, callback_url=CALLBACK)
    assert started.authorize_url == f"{AUTHENTICATE_URL}?oauth_token={REQUEST_TOKEN}"
    assert (started.state, started.secret_to_keep) == (REQUEST_TOKEN, REQUEST_SECRET)


def test_request_token_is_asked_for_write_access_with_a_signed_callback():
    transport, sent = x_answers(
        {
            REQUEST_TOKEN_URL: form(
                oauth_token=REQUEST_TOKEN,
                oauth_token_secret=REQUEST_SECRET,
                oauth_callback_confirmed="true",
            )
        }
    )
    authorization(transport).start(APP, callback_url=CALLBACK)
    (request,) = sent
    assert request.method == "POST"
    assert parse_qs(request.content.decode()) == {"x_auth_access_type": ["write"]}
    expected = authorization_header(
        "POST",
        REQUEST_TOKEN_URL,
        client=OAuth1Client(key=APP.key, secret=APP.secret),
        token=None,
        nonce=NONCE,
        timestamp=TIMESTAMP,
        parameters=[("x_auth_access_type", "write")],
        oauth_parameters={"oauth_callback": CALLBACK},
    )
    assert request.headers["Authorization"] == expected
    assert "oauth_token" not in oauth_fields(request)


def test_request_token_whose_callback_x_did_not_confirm_is_not_used():
    transport, _ = x_answers(
        {REQUEST_TOKEN_URL: form(oauth_token=REQUEST_TOKEN, oauth_token_secret=REQUEST_SECRET)}
    )
    with pytest.raises(AuthorizationUnavailable, match="did not confirm the callback"):
        authorization(transport).start(APP, callback_url=CALLBACK)


def test_request_token_answer_without_a_token_is_unavailable():
    transport, _ = x_answers({REQUEST_TOKEN_URL: form(oauth_callback_confirmed="true")})
    with pytest.raises(AuthorizationUnavailable, match="without oauth_token"):
        authorization(transport).start(APP, callback_url=CALLBACK)


def test_refused_request_token_says_the_status_and_logs_what_x_wrote(caplog):
    said = '{"errors":[{"code":32,"message":"Could not authenticate you. app-key"}]}'
    transport, _ = x_answers({REQUEST_TOKEN_URL: answering(401, said)})
    with caplog.at_level(logging.WARNING), pytest.raises(AuthorizationRefused) as refused:
        authorization(transport).start(APP, callback_url=CALLBACK)
    assert str(refused.value) == "X refused the request token: HTTP 401"
    assert "Could not authenticate you" in caplog.text


def test_server_error_is_unavailable_and_not_repeated():
    transport, _ = x_answers({REQUEST_TOKEN_URL: answering(503, "upstream <secret> page")})
    with pytest.raises(AuthorizationUnavailable) as failed:
        authorization(transport).start(APP, callback_url=CALLBACK)
    assert "secret" not in str(failed.value)
    assert "HTTP 503" in str(failed.value)


def test_unreachable_x_is_unavailable_and_names_only_the_failure():
    def refuse(request):
        raise httpx.ConnectError(f"cannot reach {request.url} with {request.headers}")

    with pytest.raises(AuthorizationUnavailable) as failed:
        authorization(httpx.MockTransport(refuse)).start(APP, callback_url=CALLBACK)
    assert str(failed.value) == "X could not be reached: ConnectError"
    assert failed.value.__cause__ is None


def test_redirect_is_not_followed():
    transport, sent = x_answers(
        {
            REQUEST_TOKEN_URL: lambda request: httpx.Response(
                302, headers={"Location": "https://evil.example/"}
            )
        }
    )
    with pytest.raises(AuthorizationUnavailable, match="HTTP 302"):
        authorization(transport).start(APP, callback_url=CALLBACK)
    assert len(sent) == 1


def test_every_url_is_https_on_x():
    for url in (REQUEST_TOKEN_URL, AUTHENTICATE_URL, ACCESS_TOKEN_URL, USERS_ME_URL):
        parts = urlsplit(url)
        assert (parts.scheme, parts.hostname) == ("https", "api.x.com")


# ----------------------------------------------------------------- finish
def test_finish_answers_the_account_and_its_access_token():
    authorized, _ = finish()
    assert authorized.account == AccountDescription(
        account_id="42",
        username="acme",
        display_name="Acme Inc.",
        picture_url="https://pbs.twimg.com/profile_images/1/acme_normal.png",
    )
    assert (authorized.access_token, authorized.access_secret) == (ACCESS_TOKEN, ACCESS_SECRET)


def test_access_token_is_signed_with_the_request_token_and_carries_the_verifier():
    _, sent = finish()
    exchange = sent[0]
    assert str(exchange.url) == ACCESS_TOKEN_URL
    assert exchange.headers["Authorization"] == authorization_header(
        "POST",
        ACCESS_TOKEN_URL,
        client=OAuth1Client(key=APP.key, secret=APP.secret),
        token=OAuth1Token(token=REQUEST_TOKEN, secret=REQUEST_SECRET),
        nonce=NONCE,
        timestamp=TIMESTAMP,
        oauth_parameters={"oauth_verifier": VERIFIER},
    )


def test_account_is_read_with_the_access_token_and_asks_for_its_fields():
    _, sent = finish()
    reading = sent[1]
    assert reading.method == "GET"
    assert reading.url.params["user.fields"] == "profile_image_url,name,username"
    assert reading.headers["Authorization"] == authorization_header(
        "GET",
        str(reading.url),
        client=OAuth1Client(key=APP.key, secret=APP.secret),
        token=OAuth1Token(token=ACCESS_TOKEN, secret=ACCESS_SECRET),
        nonce=NONCE,
        timestamp=TIMESTAMP,
    )


def test_refused_verifier_is_a_refusal():
    with pytest.raises(AuthorizationRefused, match="X refused the access token: HTTP 401"):
        finish({ACCESS_TOKEN_URL: answering(401, "Invalid request token.")})


def test_account_that_cannot_be_read_is_a_refusal():
    routes = {**FINISHED, USERS_ME_URL: answering(403, '{"title":"Client Forbidden"}')}
    with pytest.raises(AuthorizationRefused, match="X refused the account: HTTP 403"):
        finish(routes)


def test_access_token_of_another_account_than_the_one_read_is_not_used():
    routes = {
        **FINISHED,
        ACCESS_TOKEN_URL: form(
            oauth_token=ACCESS_TOKEN, oauth_token_secret=ACCESS_SECRET, user_id="7"
        ),
    }
    with pytest.raises(AuthorizationUnavailable, match="another account"):
        finish(routes)


@pytest.mark.parametrize(
    "data",
    [
        {**USER, "id": 42},
        {**USER, "id": "42; DROP"},
        {**USER, "id": "4" * 65},
        {**USER, "username": "acme‮"},
        {**USER, "username": ""},
        {key: value for key, value in USER.items() if key != "username"},
    ],
)
def test_account_without_a_usable_identifier_or_username_is_unavailable(data):
    with pytest.raises(AuthorizationUnavailable, match="usable"):
        finish({**FINISHED, USERS_ME_URL: users_me(data)})


def test_account_answer_that_is_not_json_is_unavailable():
    routes = {**FINISHED, USERS_ME_URL: answering(200, "<html>")}
    with pytest.raises(AuthorizationUnavailable, match="no JSON"):
        finish(routes)


def test_display_name_loses_control_characters_and_is_kept_within_its_bound():
    data = {**USER, "name": "Acme‮\x1b[31m" + "x" * 500}
    authorized, _ = finish({**FINISHED, USERS_ME_URL: users_me(data)})
    name = authorized.account.display_name
    assert name.startswith("Acme[31m")
    assert len(name) == 200


def test_display_name_that_is_missing_or_blank_is_the_username():
    for data in ({**USER, "name": None}, {**USER, "name": " ​ "}):
        authorized, _ = finish({**FINISHED, USERS_ME_URL: users_me(data)})
        assert authorized.account.display_name == "acme"


@pytest.mark.parametrize(
    "url",
    [
        "http://pbs.twimg.com/a.png",
        "javascript:alert(1)",
        "https://a b",
        7,
        "https://" + "a" * 2048,
    ],
)
def test_picture_that_is_not_a_bounded_https_url_is_left_out(url):
    authorized, _ = finish({**FINISHED, USERS_ME_URL: users_me({**USER, "profile_image_url": url})})
    assert authorized.account.picture_url is None


def test_tokens_and_secrets_are_not_in_repr_or_logs(caplog):
    with caplog.at_level(logging.DEBUG):
        authorized, _ = finish()
    for secret in (ACCESS_TOKEN, ACCESS_SECRET, REQUEST_SECRET, APP.secret):
        assert secret not in repr(authorized)
        assert secret not in caplog.text
    assert APP.secret not in repr(APP)
