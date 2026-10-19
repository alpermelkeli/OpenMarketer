"""Specifications of the image provider on OpenRouter, against a fake HTTP transport."""

import base64
import io
import json
import logging
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from PIL import Image

from openmarketer_core.llm_config import CatalogModel, ModelRouter
from openmarketer_core.studio.media_provider import (
    ImageInput,
    MediaAccountLimited,
    MediaProviderError,
    MediaRefused,
    MediaUnavailable,
)
from openmarketer_core.studio.openrouter_images import (
    ACCOUNT_LIMITED,
    IMAGES_ENDPOINT_ONLY,
    OpenRouterImages,
    SquareOnlyModels,
    nearest_aspect_ratio,
    padded_to_a_square,
)
from openmarketer_core.studio.square_crop import crop_safe_note

MODELS_YAML = Path(__file__).resolve().parents[4] / "config" / "models.yaml"
PROVIDER_KEY = "sk-or-v1-made-up-image-key"
ECHOED = "the provider echoed: Authorization Bearer sk-or-v1-made-up-image-key"


def image_bytes(size: tuple[int, int], fmt: str = "PNG") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (20, 120, 200)).save(buffer, format=fmt)
    return buffer.getvalue()


def data_url(data: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def reply_with(url: str | None, *, cost: Any = 0.039, **choice: Any) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": "Here is your image."}
    if url is not None:
        message["images"] = [{"type": "image_url", "image_url": {"url": url}}]
    usage = {} if cost is None else {"cost": cost}
    return {
        "model": "google/drawer-1",
        "choices": [{"message": message, "finish_reason": "stop", **choice}],
        "usage": usage,
    }


Answer = httpx.Response | Exception


def with_squares(router: ModelRouter, models: list[str]) -> ModelRouter:
    """``router`` whose configuration names ``models`` as returning squares."""
    provider = router.config.provider.model_copy(update={"image_models_returning_squares": models})
    config = router.config.model_copy(update={"provider": provider})
    return ModelRouter(config, env=router.env)


class Provider:
    """The adapter with a transport that keeps what it was sent and answers each request with
    the next of ``answers`` (one answer: every request), the last one again when they run out."""

    def __init__(
        self,
        answer: Answer | list[Answer],
        api_key: str | None = None,
        catalog: Any = None,
        square_only: SquareOnlyModels | None = None,
        configured_squares: list[str] | None = None,
    ) -> None:
        self.requests: list[httpx.Request] = []
        answers = answer if isinstance(answer, list) else [answer]

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            given = answers[min(len(self.requests), len(answers)) - 1]
            if isinstance(given, Exception):
                raise given
            return given

        router = with_squares(
            ModelRouter.from_file(MODELS_YAML, env={"OPENROUTER_API_KEY": "env-key"}),
            # None of the file's unless a test asks, so that what a test learns is its own.
            configured_squares or [],
        )
        self.images = OpenRouterImages(
            router=router,
            transport=httpx.MockTransport(handler),
            api_key=api_key,
            catalog=catalog,
            # Each test its own, so that what one learns is not another's.
            square_only=square_only or SquareOnlyModels(),
        )

    @property
    def body(self) -> dict[str, Any]:
        return json.loads(self.requests[0].content)

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(request.content) for request in self.requests]


def answering(body: dict[str, Any], status: int = 200) -> Provider:
    return Provider(httpx.Response(status, json=body))


def test_generated_image_is_returned_at_the_size_with_the_cost_the_provider_reported():
    drawn = image_bytes((1024, 1024))
    provider = answering(reply_with(data_url(drawn)))

    result = provider.images.generate("a launch banner", width=1024, height=1024)

    assert (result.data, result.mime, result.width, result.height) == (
        drawn,
        "image/png",
        1024,
        1024,
    )
    assert result.cost_usd == Decimal("0.039")
    assert (result.provider, result.model) == ("openrouter", "google/drawer-1")
    assert result.params["fitted"] == "none"
    assert result.params["cost_source"] == "provider"


def test_request_asks_the_roles_model_for_an_image_of_the_sizes_shape():
    provider = answering(reply_with(data_url(image_bytes((1080, 1350)))))

    provider.images.generate("a poster", width=1080, height=1350)

    body = provider.body
    router = provider.images.router
    assert str(provider.requests[0].url) == "https://openrouter.ai/api/v1/chat/completions"
    assert body["model"] == router.resolve("image_generator").model
    assert body["modalities"] == ["image", "text"]
    assert body["image_config"] == {"aspect_ratio": "4:5"}
    assert body["usage"] == {"include": True}
    assert "temperature" not in body and "max_tokens" not in body
    (part,) = body["messages"][0]["content"]
    assert part["type"] == "text" and "1080x1350 pixels" in part["text"]


IMAGES_ENDPOINT_404 = {
    "error": {
        "code": 404,
        "message": "qwen/qwen-image-3 is an image generation model and cannot be used with the "
        "chat/completions endpoint. Use the /api/v1/images endpoint instead.",
    }
}


ROLE = ModelRouter.from_file(MODELS_YAML, env={}).resolve("image_generator")
FIRST, FALLBACK = ROLE.model, ROLE.fallbacks[0]


def drawn_by(model: str) -> httpx.Response:
    return httpx.Response(200, json={**reply_with(data_url(image_bytes((64, 64)))), "model": model})


def test_a_request_names_the_roles_model_alone_and_no_list_of_models():
    provider = answering(reply_with(data_url(image_bytes((64, 64)))))

    provider.images.generate("x", width=64, height=64)

    assert len(provider.requests) == 1
    assert provider.body["model"] == FIRST
    assert "models" not in provider.body


@pytest.mark.parametrize(
    "failure",
    [
        httpx.Response(429, text="slow down"),
        httpx.Response(503, text="unavailable"),
        httpx.ConnectError("refused"),
        httpx.Response(404, json={"error": {"code": 404, "message": "No endpoints found"}}),
        httpx.Response(404, json=IMAGES_ENDPOINT_404),
    ],
    ids=["rate-limited", "unavailable", "unreachable", "no-endpoints", "images-endpoint-only"],
)
def test_a_failure_another_model_may_pass_is_asked_of_the_fallback(failure):
    provider = Provider([failure, drawn_by(FALLBACK)])

    result = provider.images.generate("x", width=64, height=64)

    assert [body["model"] for body in provider.bodies()] == [FIRST, FALLBACK]
    assert all("models" not in body for body in provider.bodies())
    assert result.model == FALLBACK
    assert result.params["models_failed"] == [FIRST]


@pytest.mark.parametrize(
    ("failure", "error"),
    [
        (httpx.Response(403, text="flagged by moderation"), MediaRefused),
        (httpx.Response(402, text="no credit"), MediaAccountLimited),
        (httpx.Response(400, text="bad request"), MediaProviderError),
    ],
    ids=["refused", "no-credit", "other"],
)
def test_a_refusal_or_an_account_that_cannot_pay_is_not_asked_of_another_model(failure, error):
    provider = Provider([failure, drawn_by(FALLBACK)])

    with pytest.raises(error):
        provider.images.generate("x", width=64, height=64)
    assert len(provider.requests) == 1


def test_a_refused_drawing_is_not_asked_of_another_model():
    refused = httpx.Response(200, json=reply_with(None, native_finish_reason="IMAGE_SAFETY"))
    provider = Provider([refused, drawn_by(FALLBACK)])

    with pytest.raises(MediaRefused):
        provider.images.generate("x", width=64, height=64)
    assert len(provider.requests) == 1


def test_when_every_model_failed_and_one_was_busy_the_failure_may_pass_later():
    provider = Provider(
        [httpx.Response(404, json=IMAGES_ENDPOINT_404), httpx.Response(429, text="slow down")]
    )

    with pytest.raises(MediaUnavailable):
        provider.images.generate("x", width=64, height=64)
    assert len(provider.requests) == 2


def test_each_model_is_asked_with_its_own_modalities():
    models = {FIRST: listed(FIRST, "image"), FALLBACK: listed(FALLBACK, "image", "text")}
    provider = Provider(
        [httpx.Response(503, text="busy"), drawn_by(FALLBACK)], catalog=lambda: models
    )

    provider.images.generate("x", width=64, height=64)

    assert [body["modalities"] for body in provider.bodies()] == [["image"], ["image", "text"]]


# What OpenRouter answered Seedream asked for an image and text.
NO_ENDPOINT_FOR_THE_OUTPUTS = {
    "error": {
        "code": 404,
        "message": "No endpoints found that support the requested output modalities: image, text",
    }
}


def test_a_model_with_no_endpoint_for_an_image_and_text_is_asked_once_more_for_an_image():
    provider = Provider(
        [httpx.Response(404, json=NO_ENDPOINT_FOR_THE_OUTPUTS), drawn_by(FIRST)],
        catalog=lambda: None,
    )

    result = provider.images.generate("x", width=64, height=64)

    assert [(body["model"], body["modalities"]) for body in provider.bodies()] == [
        (FIRST, ["image", "text"]),
        (FIRST, ["image"]),
    ]
    assert result.model == FIRST
    assert result.params["modalities"] == ["image"]
    assert result.params["models_failed"] == []


def test_a_model_asked_for_an_image_only_with_no_endpoint_for_it_goes_to_the_fallback():
    provider = Provider(
        [
            httpx.Response(404, json=NO_ENDPOINT_FOR_THE_OUTPUTS),
            httpx.Response(404, json=NO_ENDPOINT_FOR_THE_OUTPUTS),
            drawn_by(FALLBACK),
        ]
    )

    result = provider.images.generate("x", width=64, height=64)

    assert [(body["model"], body["modalities"]) for body in provider.bodies()] == [
        (FIRST, ["image", "text"]),
        (FIRST, ["image"]),
        (FALLBACK, ["image", "text"]),
    ]
    assert result.model == FALLBACK


def test_the_model_that_answered_is_the_one_kept_when_a_fallback_drew_it():
    fallback = ModelRouter.from_file(MODELS_YAML, env={}).resolve("image_generator").fallbacks[0]
    reply = {**reply_with(data_url(image_bytes((64, 64)))), "model": fallback}

    result = answering(reply).images.generate("x", width=64, height=64)

    assert result.model == fallback


def test_references_are_sent_as_image_parts_after_the_prompt():
    reference = image_bytes((8, 8), "JPEG")
    provider = answering(reply_with(data_url(image_bytes((64, 64)))))

    provider.images.generate(
        "x", width=64, height=64, references=[ImageInput(reference, "image/jpeg")]
    )

    text, image = provider.body["messages"][0]["content"]
    assert text["type"] == "text"
    assert image == {"type": "image_url", "image_url": {"url": data_url(reference, "image/jpeg")}}


def test_an_edit_sends_the_image_to_edit_first_and_the_instruction_last():
    original = image_bytes((64, 64))
    provider = answering(reply_with(data_url(image_bytes((64, 64)))))

    provider.images.edit(
        ImageInput(original, "image/png"), "make the sky orange", width=64, height=64
    )

    image, text = provider.body["messages"][0]["content"]
    assert image["image_url"]["url"] == data_url(original)
    assert "make the sky orange" in text["text"]


def test_an_image_of_another_shape_is_cropped_and_resized_to_the_size():
    provider = answering(reply_with(data_url(image_bytes((1024, 1024)))))

    result = provider.images.generate("x", width=1200, height=628)

    with Image.open(io.BytesIO(result.data)) as image:
        assert (image.format, image.size) == ("PNG", (1200, 628))
    assert (result.width, result.height, result.mime) == (1200, 628, "image/png")
    assert result.params["fitted"] == "cropped_and_resized"
    assert (result.params["returned_width"], result.params["returned_height"]) == (1024, 1024)


def test_an_image_of_the_same_shape_and_another_size_is_resized():
    provider = answering(reply_with(data_url(image_bytes((512, 512), "WEBP"), "image/webp")))

    result = provider.images.generate("x", width=1024, height=1024)

    with Image.open(io.BytesIO(result.data)) as image:
        assert image.size == (1024, 1024)
    assert result.params["fitted"] == "resized"


def test_the_nearest_supported_aspect_ratio_is_asked_for():
    assert nearest_aspect_ratio(1080, 1080) == "1:1"
    assert nearest_aspect_ratio(1080, 1920) == "9:16"
    assert nearest_aspect_ratio(1200, 628) == "16:9"


def test_a_reply_without_an_image_is_its_own_error():
    provider = answering(reply_with(None))

    with pytest.raises(MediaProviderError, match="returned no image") as raised:
        provider.images.generate("x", width=64, height=64)
    assert type(raised.value) is MediaProviderError


def test_a_reply_without_an_image_for_safety_is_a_refusal():
    provider = answering(reply_with(None, native_finish_reason="IMAGE_SAFETY"))

    with pytest.raises(MediaRefused):
        provider.images.generate("x", width=64, height=64)


@pytest.mark.parametrize(
    "url",
    [
        data_url(b"<html>not an image</html>"),
        "data:image/png;base64,***not base64***",
        "https://cdn.example.com/image.png",
        data_url(image_bytes((8, 8)), "image/gif"),
    ],
    ids=["bytes-not-an-image", "not-base64", "not-a-data-url", "other-type"],
)
def test_what_is_not_an_accepted_image_is_refused(url):
    provider = answering(reply_with(url))

    with pytest.raises(MediaProviderError):
        provider.images.generate("x", width=64, height=64)


def test_what_the_provider_wrote_is_logged_and_never_in_the_error(caplog):
    provider = Provider(httpx.Response(400, text=ECHOED), api_key=PROVIDER_KEY)

    with caplog.at_level(logging.WARNING), pytest.raises(MediaProviderError) as raised:
        provider.images.generate("x", width=64, height=64)

    assert str(raised.value) == "the image provider answered HTTP 400"
    assert "echoed" in caplog.text


def test_an_error_reported_in_a_reply_is_not_quoted():
    provider = answering({"error": {"code": 400, "message": ECHOED}})

    with pytest.raises(MediaProviderError) as raised:
        provider.images.generate("x", width=64, height=64)
    assert "echoed" not in str(raised.value)


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        (httpx.Response(429, text="slow down"), MediaUnavailable),
        (httpx.Response(502, text="bad gateway"), MediaUnavailable),
        (httpx.ConnectError("refused"), MediaUnavailable),
        (httpx.Response(403, text="flagged by moderation"), MediaRefused),
        (httpx.Response(402, text="no credit"), MediaAccountLimited),
        (httpx.Response(400, text="bad request"), MediaProviderError),
    ],
    ids=["rate-limited", "server-error", "unreachable", "moderated", "no-credit", "other"],
)
def test_a_failure_is_told_by_whether_it_may_pass(answer, error):
    provider = Provider(answer)

    with pytest.raises(error) as raised:
        provider.images.generate("x", width=64, height=64)
    if error is MediaProviderError:
        assert not isinstance(raised.value, MediaUnavailable | MediaRefused)


# What OpenRouter answered a live run whose key could not pay for image output.
KEY_LIMIT_ERROR = {
    "error": {
        "message": "This request requires at least $1.00 in balance for image or video output",
        "code": 402,
        "metadata": {"limit_source": "openrouter_key_limit"},
    }
}


@pytest.mark.parametrize(
    "provider",
    [
        Provider(httpx.Response(402, json=KEY_LIMIT_ERROR)),
        answering(KEY_LIMIT_ERROR),
        answering({"error": {"message": "over the limit", "metadata": {"limit_source": "key"}}}),
    ],
    ids=["http-402", "402-in-a-reply", "a-limit-named"],
)
def test_a_balance_or_key_limit_too_low_is_told_in_our_own_words(provider):
    with pytest.raises(MediaAccountLimited) as raised:
        provider.images.generate("x", width=64, height=64)

    assert str(raised.value) == ACCOUNT_LIMITED
    assert "$1.00" not in str(raised.value)


def test_a_rate_limit_that_names_its_limit_still_passes():
    limited = {"error": {"code": 429, "metadata": {"limit_source": "requests_per_minute"}}}

    with pytest.raises(MediaUnavailable):
        Provider(httpx.Response(429, json=limited)).images.generate("x", width=64, height=64)


def test_without_a_reported_cost_the_estimate_is_kept_and_said_so():
    provider = answering(reply_with(data_url(image_bytes((64, 64))), cost=None))

    result = provider.images.generate("x", width=64, height=64)

    assert result.cost_usd == provider.images.estimate("generate")
    assert result.params["cost_source"] == "estimate"


def test_the_estimate_is_the_roles_estimate_per_image():
    provider = answering({})
    expected = provider.images.router.resolve("image_generator").estimate_usd_per_image
    assert provider.images.estimate("edit") == Decimal(str(expected))


def test_the_key_handed_in_is_sent_and_kept_out_of_repr_and_errors():
    provider = Provider(httpx.Response(401, text=ECHOED), api_key=PROVIDER_KEY)

    with pytest.raises(MediaProviderError) as raised:
        provider.images.generate("x", width=64, height=64)

    assert provider.requests[0].headers["authorization"] == f"Bearer {PROVIDER_KEY}"
    assert PROVIDER_KEY not in repr(provider.images)
    assert PROVIDER_KEY not in str(raised.value)


def test_a_size_beyond_the_limits_is_refused_before_anything_is_sent():
    provider = answering({})

    with pytest.raises(ValueError):
        provider.images.generate("x", width=20_000, height=64)
    assert provider.requests == []


def listed(slug: str, *outputs: str) -> CatalogModel:
    return CatalogModel(
        id=slug,
        name=slug,
        context_length=None,
        prompt_price=None,
        completion_price=None,
        supported_parameters=frozenset(),
        input_modalities=frozenset({"text", "image"}),
        output_modalities=frozenset(outputs),
    )


def catalogue_of_the_roles_models(*outputs: str) -> dict[str, CatalogModel]:
    resolved = ModelRouter.from_file(MODELS_YAML, env={}).resolve("image_generator")
    return {slug: listed(slug, *outputs) for slug in [resolved.model, *resolved.fallbacks]}


def test_models_that_make_images_only_are_asked_for_an_image_only():
    models = catalogue_of_the_roles_models("image")
    provider = Provider(
        httpx.Response(200, json=reply_with(data_url(image_bytes((64, 64))))),
        catalog=lambda: models,
    )

    provider.images.generate("x", width=64, height=64)

    assert provider.body["modalities"] == ["image"]


def test_models_that_also_write_text_are_asked_for_an_image_and_text():
    models = catalogue_of_the_roles_models("image", "text")
    provider = Provider(
        httpx.Response(200, json=reply_with(data_url(image_bytes((64, 64))))),
        catalog=lambda: models,
    )

    provider.images.generate("x", width=64, height=64)

    assert provider.body["modalities"] == ["image", "text"]


def test_a_catalogue_that_cannot_be_read_leaves_the_request_as_it_was_and_is_logged(caplog):
    provider = Provider(
        httpx.Response(200, json=reply_with(data_url(image_bytes((64, 64))))),
        catalog=lambda: None,
    )

    with caplog.at_level(logging.WARNING):
        result = provider.images.generate("x", width=64, height=64)

    assert result.data
    assert provider.body["modalities"] == ["image", "text"]
    assert "model catalogue not available" in caplog.text


def test_the_reply_of_a_model_that_makes_images_only_is_read():
    drawn = image_bytes((64, 64))
    reply = {
        "model": "black-forest-labs/flux-3-image",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "images": [{"type": "image_url", "image_url": {"url": data_url(drawn)}}],
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"cost": 0.04},
    }

    result = answering(reply).images.generate("x", width=64, height=64)

    assert result.data == drawn


def test_an_image_given_as_a_url_text_or_after_an_unreadable_entry_is_read():
    drawn = image_bytes((64, 64))
    reply = reply_with(None)
    reply["choices"][0]["message"]["images"] = [
        {"type": "image_url"},
        {"type": "image_url", "image_url": data_url(drawn)},
    ]

    assert answering(reply).images.generate("x", width=64, height=64).data == drawn


def test_an_image_in_the_messages_content_parts_is_read_when_images_has_none():
    drawn = image_bytes((64, 64))
    reply = reply_with(None)
    reply["choices"][0]["message"]["content"] = [
        {"type": "text", "text": "Here it is."},
        {"type": "image_url", "image_url": {"url": data_url(drawn)}},
    ]

    assert answering(reply).images.generate("x", width=64, height=64).data == drawn


def test_a_model_served_on_the_images_endpoint_only_is_told_in_our_own_words():
    provider = answering(IMAGES_ENDPOINT_404, status=404)

    with pytest.raises(MediaProviderError) as failure:
        provider.images.generate("x", width=64, height=64)

    assert str(failure.value) == IMAGES_ENDPOINT_ONLY
    assert type(failure.value) is MediaProviderError
    assert "qwen" not in str(failure.value)


def test_another_404_is_still_the_providers_http_error():
    provider = answering({"error": {"code": 404, "message": "No endpoints found"}}, status=404)

    with pytest.raises(MediaProviderError) as failure:
        provider.images.generate("x", width=64, height=64)

    assert str(failure.value) == "the image provider answered HTTP 404"


# ------------------------------------------------------------------ models that return squares
def picture(size: tuple[int, int], colour: tuple[int, int, int] = (200, 40, 40)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="PNG")
    return buffer.getvalue()


def sent_images(body: dict[str, Any]) -> list[Image.Image]:
    parts = [part for part in body["messages"][0]["content"] if part["type"] == "image_url"]
    return [
        Image.open(io.BytesIO(base64.b64decode(part["image_url"]["url"].split(",", 1)[1])))
        for part in parts
    ]


SQUARE_REPLY = httpx.Response(200, json=reply_with(data_url(image_bytes((2048, 2048)))))
PORTRAIT_REFERENCE = ImageInput(picture((1080, 1350)), "image/png")
SQUARE_REFERENCE = ImageInput(picture((256, 256)), "image/png")


def test_a_model_that_returns_a_square_for_a_portrait_is_remembered_and_its_image_cropped():
    known = SquareOnlyModels()
    provider = Provider(SQUARE_REPLY, square_only=known)

    result = provider.images.generate("a poster", width=1080, height=1350)

    assert known.returns_squares(FIRST)
    assert (result.width, result.height) == (1080, 1350)
    assert Image.open(io.BytesIO(result.data)).size == (1080, 1350)
    assert result.params["fitted"] == "cropped_and_resized"
    assert result.params["images_padded"] == 0


def test_a_model_known_to_return_squares_is_sent_the_references_of_the_shape_padded():
    known = SquareOnlyModels()
    known.remember(FIRST)
    provider = Provider(SQUARE_REPLY, square_only=known)

    result = provider.images.generate(
        "a poster", width=1080, height=1350, references=[PORTRAIT_REFERENCE, SQUARE_REFERENCE]
    )

    padded, square = sent_images(provider.body)
    assert padded.size == (1350, 1350)
    assert square.size == (256, 256)
    assert result.params["images_padded"] == 1


def test_an_edit_for_a_square_only_model_pads_the_image_it_changes():
    known = SquareOnlyModels()
    known.remember(FIRST)
    provider = Provider(SQUARE_REPLY, square_only=known)

    result = provider.images.edit(PORTRAIT_REFERENCE, "brighter", width=1080, height=1350)

    [changed] = sent_images(provider.body)
    assert changed.size == (1350, 1350)
    assert result.params["images_padded"] == 1


def test_a_model_not_known_to_return_squares_is_sent_the_references_as_they_are():
    provider = Provider(SQUARE_REPLY)

    provider.images.generate("a poster", width=1080, height=1350, references=[PORTRAIT_REFERENCE])

    [sent] = sent_images(provider.body)
    assert sent.size == (1080, 1350)


def test_a_square_asset_pads_nothing_and_learns_nothing():
    known = SquareOnlyModels()
    provider = Provider(SQUARE_REPLY, square_only=known)

    result = provider.images.generate("a post", width=1080, height=1080)

    assert not known.returns_squares(FIRST)
    assert result.params["images_padded"] == 0


def test_a_model_that_returns_the_shape_asked_is_forgotten():
    known = SquareOnlyModels()
    known.remember(FIRST)
    provider = answering(reply_with(data_url(image_bytes((1080, 1350)))))
    provider.images.square_only = known

    provider.images.generate("a poster", width=1080, height=1350)

    assert not known.returns_squares(FIRST)


def test_the_result_says_whether_the_prompt_had_the_crop_note():
    provider = Provider(SQUARE_REPLY)
    note = crop_safe_note(1080, 1350)

    told = provider.images.generate(f"a poster\n\n{note}", width=1080, height=1350)
    untold = provider.images.generate("a poster", width=1080, height=1350)

    assert told.params["crop_safe_note"] is True
    assert untold.params["crop_safe_note"] is False


def test_a_padded_image_is_centred_on_a_square_of_its_longer_side():
    padded = Image.open(io.BytesIO(padded_to_a_square(picture((400, 500))) or b""))

    assert padded.size == (500, 500)
    # The image itself fills the central 80% of the width; the strips carry its colour.
    assert padded.getpixel((250, 250)) == (200, 40, 40)
    assert padded.getpixel((50, 0)) == (200, 40, 40)
    assert padded.getpixel((2, 250)) == (200, 40, 40)


def test_a_padded_image_keeps_its_layout_in_the_region_the_crop_keeps():
    layout = Image.new("RGB", (400, 500), (255, 255, 255))
    layout.paste((0, 0, 0), (0, 0, 400, 50))  # a band along the top
    buffer = io.BytesIO()
    layout.save(buffer, format="PNG")

    padded = Image.open(io.BytesIO(padded_to_a_square(buffer.getvalue()) or b"")).convert("RGB")

    assert padded.getpixel((50, 10)) == (0, 0, 0)  # the band's left end, at x 50 of 500
    assert padded.getpixel((449, 10)) == (0, 0, 0)
    assert padded.getpixel((250, 300)) == (255, 255, 255)


def test_a_square_image_is_not_padded():
    assert padded_to_a_square(picture((300, 300))) is None


def test_a_large_padded_image_is_scaled_to_the_longest_side_a_square_only_model_returns():
    padded = Image.open(io.BytesIO(padded_to_a_square(picture((1290, 2796))) or b""))
    assert padded.size == (2048, 2048)


def test_the_models_remembered_as_returning_squares_are_bounded():
    known = SquareOnlyModels(limit=2)
    for model in ("a", "b", "c"):
        known.remember(model)

    assert not known.returns_squares("a")
    assert known.returns_squares("b") and known.returns_squares("c")


def test_a_configured_square_only_model_is_sent_padded_references_on_its_first_image():
    provider = Provider(SQUARE_REPLY, configured_squares=[FIRST])

    result = provider.images.generate(
        "a poster", width=1080, height=1350, references=[PORTRAIT_REFERENCE]
    )

    [sent] = sent_images(provider.body)
    assert sent.size == (1350, 1350)
    assert result.params["images_padded"] == 1


def test_the_file_names_the_role_model_as_returning_squares():
    router = ModelRouter.from_file(MODELS_YAML, env={})

    assert FIRST in router.config.provider.image_models_returning_squares


def test_an_unconfigured_model_still_learns_that_it_returns_squares():
    known = SquareOnlyModels()
    provider = Provider(SQUARE_REPLY, square_only=known, configured_squares=["other/drawer"])

    provider.images.generate("a poster", width=1080, height=1350, references=[PORTRAIT_REFERENCE])
    provider.images.generate("a poster", width=1080, height=1350, references=[PORTRAIT_REFERENCE])

    first, second = provider.bodies()
    assert [image.size for image in sent_images(first)] == [(1080, 1350)]
    assert [image.size for image in sent_images(second)] == [(1350, 1350)]
    assert known.returns_squares("other/drawer") and known.returns_squares(FIRST)
