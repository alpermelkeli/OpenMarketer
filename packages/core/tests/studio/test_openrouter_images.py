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
    nearest_aspect_ratio,
)

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


class Provider:
    """The adapter with a transport that answers once and keeps what it was sent."""

    def __init__(
        self, answer: httpx.Response | Exception, api_key: str | None = None, catalog: Any = None
    ) -> None:
        self.requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if isinstance(answer, Exception):
                raise answer
            return answer

        router = ModelRouter.from_file(MODELS_YAML, env={"OPENROUTER_API_KEY": "env-key"})
        self.images = OpenRouterImages(
            router=router,
            transport=httpx.MockTransport(handler),
            api_key=api_key,
            catalog=catalog,
        )

    @property
    def body(self) -> dict[str, Any]:
        return json.loads(self.requests[0].content)


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


def test_request_offers_the_roles_fallbacks_after_its_model():
    provider = answering(reply_with(data_url(image_bytes((64, 64)))))

    provider.images.generate("x", width=64, height=64)

    resolved = provider.images.router.resolve("image_generator")
    assert provider.body["models"] == [resolved.model, *resolved.fallbacks]


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


IMAGES_ENDPOINT_404 = {
    "error": {
        "code": 404,
        "message": "qwen/qwen-image-3 is an image generation model and cannot be used with the "
        "chat/completions endpoint. Use the /api/v1/images endpoint instead.",
    }
}


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
