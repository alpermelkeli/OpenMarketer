"""Tests for X's publishing adapter, over a fake transport: nothing reaches X."""

import json
import logging
from datetime import UTC, datetime

import httpx
import pytest

from openmarketer_core.social_accounts.connection import AccountTokens, PlatformApp
from openmarketer_core.social_accounts.oauth1 import (
    OAuth1Client,
    OAuth1Token,
    authorization_header,
)
from openmarketer_core.social_accounts.publishing import (
    PostImageFile,
    PublishedPost,
    PublishingAccessRevoked,
    PublishingAccount,
    PublishingRefused,
    PublishingUnavailable,
    RefusalReason,
)
from openmarketer_core.social_accounts.x.publishing import (
    CREATE_POST_URL,
    MAX_IMAGE_BYTES,
    MEDIA_UPLOAD_URL,
    XPublisher,
    same_text,
)

APP = PlatformApp(key="app-key", secret="app-secret")
TOKENS = AccountTokens(access_token="42-access-token", access_secret="access-secret")
ACCOUNT = PublishingAccount(account_id="42", username="acme")
NONCE = "fixed-nonce"
TIMESTAMP = 1700000000
TEXT = "Your notes keep up offline. Write anywhere."
IMAGE = PostImageFile(data=b"\x89PNG-the-image", media_type="image/png")
POSTS_URL = "https://api.x.com/2/users/42/tweets"
SINCE = datetime(2026, 11, 11, 11, 55, tzinfo=UTC)
CREATED = {"data": {"id": "1850000000000000001", "text": TEXT}}


def x_answers(routes):
    """A transport that answers each URL (without its query) from ``routes`` and keeps requests."""
    sent: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return routes[str(request.url).split("?")[0]](request)

    return httpx.MockTransport(handle), sent


def answering(status, body=None, headers=None):
    def answer(request: httpx.Request) -> httpx.Response:
        if body is None:
            return httpx.Response(status, headers=headers)
        return httpx.Response(status, json=body, headers=headers)

    return answer


def publisher(transport):
    return XPublisher(transport=transport, nonce=lambda: NONCE, clock=lambda: TIMESTAMP)


def publish(routes, *, image=None):
    transport, sent = x_answers(routes)
    return publisher(transport).publish(APP, TOKENS, ACCOUNT, text=TEXT, image=image), sent


def find(posts, *, status=200):
    transport, sent = x_answers({POSTS_URL: answering(status, posts)})
    found = publisher(transport).find_published(APP, TOKENS, ACCOUNT, text=TEXT, since=SINCE)
    return found, sent


def signed_as_the_account(url: str) -> str:
    """The header of a request whose body is not signed: only the query and oauth_* ones."""
    return authorization_header(
        "POST" if "tweets?" not in url else "GET",
        url,
        client=OAuth1Client(key=APP.key, secret=APP.secret),
        token=OAuth1Token(token=TOKENS.access_token, secret=TOKENS.access_secret),
        nonce=NONCE,
        timestamp=TIMESTAMP,
    )


def problem(status, detail, title="Forbidden"):
    return {"title": title, "detail": detail, "type": "about:blank", "status": status}


# ---------------------------------------------------------------------------- publishing
def test_a_text_post_is_created_and_answered_with_its_address():
    published, [request] = publish({CREATE_POST_URL: answering(201, CREATED)})

    assert published == PublishedPost(
        external_id="1850000000000000001", url="https://x.com/acme/status/1850000000000000001"
    )
    assert json.loads(request.content) == {"text": TEXT}
    assert request.headers["Content-Type"] == "application/json"


def test_a_posts_json_body_is_not_part_of_its_signature():
    _, [request] = publish({CREATE_POST_URL: answering(201, CREATED)})

    assert request.headers["Authorization"] == signed_as_the_account(CREATE_POST_URL)


def test_an_image_is_uploaded_first_and_attached_to_the_post():
    routes = {
        MEDIA_UPLOAD_URL: answering(200, {"data": {"id": "710", "media_key": "3_710"}}),
        CREATE_POST_URL: answering(201, CREATED),
    }

    published, [upload, post] = publish(routes, image=IMAGE)

    assert published.external_id == "1850000000000000001"
    assert upload.headers["Content-Type"].startswith("multipart/form-data; boundary=")
    body = upload.content
    assert b'name="media_category"' in body and b"tweet_image" in body
    assert b'name="media"; filename="image"' in body and IMAGE.data in body
    assert json.loads(post.content) == {"text": TEXT, "media": {"media_ids": ["710"]}}


def test_a_multipart_upload_is_signed_without_its_body():
    routes = {
        MEDIA_UPLOAD_URL: answering(200, {"data": {"id": "710"}}),
        CREATE_POST_URL: answering(201, CREATED),
    }

    _, [upload, _] = publish(routes, image=IMAGE)

    assert upload.headers["Authorization"] == signed_as_the_account(MEDIA_UPLOAD_URL)


@pytest.mark.parametrize(
    "image",
    [
        PostImageFile(data=b"<svg/>", media_type="image/svg+xml"),
        PostImageFile(data=b"0" * (MAX_IMAGE_BYTES + 1), media_type="image/png"),
    ],
)
def test_an_image_x_does_not_take_is_refused_before_anything_is_sent(image):
    transport, sent = x_answers({})

    with pytest.raises(PublishingRefused) as refused:
        publisher(transport).publish(APP, TOKENS, ACCOUNT, text=TEXT, image=image)

    assert refused.value.reason is RefusalReason.MEDIA_REFUSED
    assert sent == []


def test_an_upload_x_refuses_is_a_refused_image_and_no_post_is_created():
    routes = {MEDIA_UPLOAD_URL: answering(400, problem(400, "Invalid media", "Bad Request"))}

    with pytest.raises(PublishingRefused) as refused:
        publish(routes, image=IMAGE)

    assert refused.value.reason is RefusalReason.MEDIA_REFUSED


def test_a_duplicate_post_is_told_apart_from_other_refusals():
    detail = "You are not allowed to create a Tweet with duplicate content."
    routes = {CREATE_POST_URL: answering(403, problem(403, detail))}

    with pytest.raises(PublishingRefused) as refused:
        publish(routes)

    assert refused.value.duplicate
    assert "duplicate content" not in str(refused.value)


def test_the_v1_duplicate_code_is_a_duplicate_too():
    body = {"errors": [{"code": 187, "message": "Status is a duplicate."}]}

    with pytest.raises(PublishingRefused) as refused:
        publish({CREATE_POST_URL: answering(403, body)})

    assert refused.value.reason is RefusalReason.DUPLICATE


@pytest.mark.parametrize(
    ("status", "body", "reason"),
    [
        (400, problem(400, "Your Tweet text is too long.", "Bad Request"), RefusalReason.TOO_LONG),
        (
            403,
            problem(403, "You are not permitted to perform this action."),
            RefusalReason.NOT_PERMITTED,
        ),
        (
            402,
            problem(402, "Your account has no credits.", "Payment Required"),
            RefusalReason.OUT_OF_CREDITS,
        ),
        (400, problem(400, "Something else.", "Bad Request"), RefusalReason.REFUSED),
        (422, None, RefusalReason.REFUSED),
    ],
)
def test_a_refusal_is_read_as_a_category(status, body, reason):
    with pytest.raises(PublishingRefused) as refused:
        publish({CREATE_POST_URL: answering(status, body)})

    assert refused.value.reason is reason


def test_a_token_x_no_longer_accepts_asks_for_the_account_to_be_connected_again():
    with pytest.raises(PublishingAccessRevoked, match="connect the account again"):
        publish({CREATE_POST_URL: answering(401, problem(401, "Unauthorized", "Unauthorized"))})


def test_a_rate_limit_is_unavailable_with_how_long_x_asked_to_wait():
    reset = {"x-rate-limit-reset": str(TIMESTAMP + 90)}

    with pytest.raises(PublishingUnavailable) as unavailable:
        publish({CREATE_POST_URL: answering(429, problem(429, "Too Many Requests"), reset)})

    assert unavailable.value.retry_after_seconds == 90


def test_a_server_error_is_unavailable():
    with pytest.raises(PublishingUnavailable, match="HTTP 503"):
        publish({CREATE_POST_URL: answering(503)})


def test_a_request_whose_answer_never_came_is_unavailable_without_its_text():
    def times_out(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(f"timed out reading {request.headers['Authorization']}")

    with pytest.raises(PublishingUnavailable) as unavailable:
        publish({CREATE_POST_URL: times_out})

    assert str(unavailable.value) == "X could not be reached: ReadTimeout"


@pytest.mark.parametrize("post_id", ["18/../1", "", None, "1" * 65])
def test_a_post_id_that_is_not_digits_is_not_used_for_an_address(post_id):
    body = {"data": {"id": post_id, "text": TEXT}}

    with pytest.raises(PublishingUnavailable, match="usable id"):
        publish({CREATE_POST_URL: answering(201, body)})


def test_what_x_says_when_it_refuses_is_logged_and_kept_out_of_the_error(caplog):
    detail = "Reason only X knows: tok-123"
    caplog.set_level(logging.WARNING)

    with pytest.raises(PublishingRefused) as refused:
        publish({CREATE_POST_URL: answering(400, problem(400, detail, "Bad Request"))})

    assert "tok-123" not in str(refused.value)
    assert "tok-123" in caplog.text


def test_an_account_whose_identifier_is_not_digits_is_never_put_in_a_path():
    transport, sent = x_answers({})

    with pytest.raises(PublishingRefused):
        publisher(transport).find_published(
            APP, TOKENS, PublishingAccount("42/../me", "acme"), text=TEXT, since=SINCE
        )

    assert sent == []


# ------------------------------------------------------------------------------ the lookup
def test_the_lookup_reads_the_five_latest_posts_since_the_send_without_replies_or_reposts():
    _, [request] = find({"meta": {"result_count": 0}})

    assert dict(request.url.params) == {
        "max_results": "5",
        "exclude": "replies,retweets",
        "start_time": "2026-11-11T11:55:00Z",
    }
    assert request.headers["Authorization"] == signed_as_the_account(str(request.url))


def test_the_lookup_finds_the_post_with_the_text():
    posts = {
        "data": [
            {"id": "1850000000000000003", "text": "Another post."},
            {"id": "1850000000000000002", "text": TEXT},
        ]
    }

    found, _ = find(posts)

    assert found == PublishedPost(
        external_id="1850000000000000002", url="https://x.com/acme/status/1850000000000000002"
    )


def test_the_lookup_finds_nothing_when_no_post_has_the_text():
    found, _ = find({"data": [{"id": "1850000000000000003", "text": "Another post."}]})

    assert found is None


def test_the_lookup_finds_nothing_when_the_account_has_no_post_since():
    found, _ = find({"meta": {"result_count": 0}})

    assert found is None


def test_the_lookup_is_refused_like_a_send():
    with pytest.raises(PublishingAccessRevoked):
        find(problem(401, "Unauthorized", "Unauthorized"), status=401)


def test_a_post_with_an_image_is_found_although_x_shows_the_images_address_in_its_text():
    assert same_text("Notes & more <3", "Notes &amp; more &lt;3 https://t.co/AbC123xyz")


def test_a_text_that_differs_beyond_xs_escaping_is_another_post():
    assert not same_text("Notes & more", "Notes & more!")
