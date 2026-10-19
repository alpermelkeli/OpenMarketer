"""The ``MediaProvider`` port answered by an image model on OpenRouter's chat endpoint.

One request per model: the role's model (``image_generator`` by default, resolved by
``ModelRouter``) is sent the prompt, or the image to edit and the instruction, and any
references as image parts (``llm.image_part``), and asked for an image back with its own
``modalities``: ``["image"]`` when the provider's catalogue says it makes images only,
``["image", "text"]`` otherwise or when the catalogue cannot be read
(``llm_config.image_modalities``); a model asked for both that OpenRouter answers has no
endpoint for those outputs (HTTP 404) is asked once more for ``["image"]``.

The role's fallbacks are tried here, one request each, in order, and never sent as
OpenRouter's ``models`` list: given one, OpenRouter answered from the fallback although the
first model answers alone. The next model is tried only after a failure another model may
pass (``_another_model_may_pass``): the provider unreachable or busy, a rate limit, a model
with no endpoint (HTTP 404) or served on the images endpoint only. A refusal of the content
and an account that cannot pay end it, since every model would answer the same. When every
model failed, the failure raised is ``MediaUnavailable`` if one of them was (it may pass
later), and the first model's otherwise. The result names the model that answered and the
ones tried before it (``params["models_failed"]``).

The shape is asked for with OpenRouter's
``image_config.aspect_ratio``, the supported ratio nearest the size; a model that ignores it is
still held to the size, since what comes back is fitted to it here: cropped about its centre to
the size's proportions and resized, and the result says so (``params["fitted"]``).

What comes back is the model's. OpenRouter gives a model's image in the reply's
``choices[0].message.images`` whether the model also writes text or not; the first entry that
holds a URL is read there, and in the message's content parts when ``images`` has none. An
image is kept only when it is a data URL of a PNG, JPEG or WebP whose bytes
``output_check.check_image`` accepts as that type, within the same limits as a sandbox's
output. The cost is what the provider reports (``usage.cost``); without it, the role's
estimate, and the result says which (``params["cost_source"]``).

What the provider writes (an error page, a refusal's text, a reply without an image) is logged,
cut short, and never put in an error: an error is a fixed sentence. An answer that the key's
balance or usage limit is too low (HTTP 402, or an error that names the limit it hit) is
``MediaAccountLimited``, with a sentence that says where to raise it. A model OpenRouter serves
only on its images endpoint (HTTP 404 that says so: Qwen Image, FLUX 3, GPT Image) is told in a
sentence of its own (``IMAGES_ENDPOINT_ONLY``): this adapter uses the chat endpoint only. The
provider's key is handed in by the caller that resolved it, or is the environment's, and is
kept out of ``repr``.
"""

from __future__ import annotations

import base64
import binascii
import io
import logging
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from PIL import Image, ImageOps

from openmarketer_core.llm import image_part, model_name_to_keep, reported_status
from openmarketer_core.llm_config import IMAGE_ONLY, CatalogModel, ModelRouter
from openmarketer_core.studio.media_provider import (
    ImageInput,
    MediaAccountLimited,
    MediaOperation,
    MediaProviderError,
    MediaRefused,
    MediaResult,
    MediaUnavailable,
)
from openmarketer_core.studio.output_check import OutputLimits, RawOutput, check_image
from openmarketer_core.studio.sandbox import ImageMime

logger = logging.getLogger(__name__)

IMAGE_ROLE = "image_generator"

# The ratios OpenRouter's image_config.aspect_ratio takes, as width over height.
ASPECT_RATIOS: dict[str, float] = {
    "1:1": 1.0,
    "2:3": 2 / 3,
    "3:2": 3 / 2,
    "3:4": 3 / 4,
    "4:3": 4 / 3,
    "4:5": 4 / 5,
    "5:4": 5 / 4,
    "9:16": 9 / 16,
    "16:9": 16 / 9,
    "21:9": 21 / 9,
}

# How far a returned image's proportions may be from the size's and still be only resized.
ASPECT_TOLERANCE = 0.01

# A reply carries its image as base64 text, a third larger than the bytes.
MAX_REPLY_BYTES = 64 * 1024 * 1024

_PASSING_STATUSES = (408, 429)
# OpenRouter answers 402 when the account's credits or the key's limit cannot pay for a request;
# the error of a limit names it in ``metadata.limit_source``.
_ACCOUNT_LIMITED_STATUS = 402
ACCOUNT_LIMITED = (
    "the OpenRouter key's balance or usage limit is too low for image output; "
    "raise it at openrouter.ai/settings/keys"
)
# OpenRouter answers 404 for a model it serves on /api/v1/images only, and says so in its error.
_NOT_ON_THIS_ENDPOINT_STATUS = 404
_IMAGES_ENDPOINT_MARKS = ("/images endpoint", "api/v1/images", "image generation model")
IMAGES_ENDPOINT_ONLY = (
    "this image model is served by OpenRouter's images endpoint, which the studio does not use "
    "yet; choose another image model for the image_generator role in Settings"
)
# OpenRouter answers 404 "No endpoints found that support the requested output modalities" to a
# model of images only that is asked for an image and text.
_OUTPUTS_MARK = "output modalities"
# OpenRouter answers 403 when its moderation flags the input.
_REFUSED_STATUSES = (403,)
# The finish reasons a model gives when it would not draw what was asked.
_REFUSAL_REASONS = ("content_filter", "safety", "prohibited", "blocklist", "image_safety")

_DATA_URL_PREFIXES = {
    "data:image/png;base64,": "image/png",
    "data:image/jpeg;base64,": "image/jpeg",
    "data:image/webp;base64,": "image/webp",
}


@dataclass
class OpenRouterImages:
    """``MediaProvider`` backed by the per-role model configuration.

    ``api_key`` is the provider's key, for a caller that resolved one itself; without it the
    key is the router's environment's. ``limits`` bound what is accepted back and the size
    that may be asked for. ``catalog`` gives the provider's catalogue, or ``None`` when it
    cannot be read now (``llm_catalog.ModelCatalog.models``); without it every request asks
    for an image and text, which a model that makes images only may refuse.
    """

    router: ModelRouter
    role: str = IMAGE_ROLE
    transport: httpx.BaseTransport | None = None
    limits: OutputLimits = field(default_factory=OutputLimits)
    api_key: str | None = field(default=None, repr=False)
    catalog: Callable[[], Mapping[str, CatalogModel] | None] | None = field(
        default=None, repr=False
    )

    def generate(
        self, prompt: str, *, width: int, height: int, references: Sequence[ImageInput] = ()
    ) -> MediaResult:
        text = f"{prompt}\n\nThe image is {width}x{height} pixels."
        if references:
            text += " The images attached are references to follow."
        content = [{"type": "text", "text": text}, *_parts(references)]
        return self._draw("generate", content, width=width, height=height)

    def edit(
        self,
        image: ImageInput,
        instruction: str,
        *,
        width: int,
        height: int,
        references: Sequence[ImageInput] = (),
    ) -> MediaResult:
        text = f"Edit the first image: {instruction}\n\nThe result is {width}x{height} pixels."
        if references:
            text += " The other images attached are references to follow."
        content = [*_parts([image, *references]), {"type": "text", "text": text}]
        return self._draw("edit", content, width=width, height=height)

    def estimate(self, operation: MediaOperation) -> Decimal:
        """The role's ``estimate_usd_per_image``, the same for both operations.

        Raises ``MediaProviderError`` when the configuration gives none: a budget cannot hold
        a figure it does not have.
        """
        estimate = self.router.resolve(self.role).estimate_usd_per_image
        if estimate is None:
            raise MediaProviderError(f"the role {self.role} gives no estimate_usd_per_image")
        return Decimal(str(estimate))

    def _draw(
        self, operation: MediaOperation, content: list[dict[str, Any]], *, width: int, height: int
    ) -> MediaResult:
        self._check_size(width, height)
        aspect_ratio = nearest_aspect_ratio(width, height)
        catalogue = self._catalogue()
        failures: list[MediaProviderError] = []
        failed: list[str] = []
        for model in self.router.image_models(self.role):
            try:
                body, modalities = self._ask(model, content, aspect_ratio, catalogue)
                drawn = self._image_in(body)
            except MediaProviderError as e:
                if not _another_model_may_pass(e):
                    raise
                logger.warning("image model %s failed (%s); trying the next one", model, e)
                failures.append(e)
                failed.append(model)
                continue
            return self._result(
                operation,
                body,
                drawn,
                asked_for=model,
                width=width,
                height=height,
                params={
                    "aspect_ratio": aspect_ratio,
                    "modalities": modalities,
                    "models_failed": failed,
                },
            )
        raise _of_all(failures)

    def _ask(
        self,
        model: str,
        content: list[dict[str, Any]],
        aspect_ratio: str,
        catalogue: Mapping[str, CatalogModel] | None,
    ) -> tuple[dict[str, Any], list[str]]:
        """The reply of ``model``, and the modalities it was asked for; asked once more for an
        image only when it has no endpoint for an image and text."""

        def payload(modalities: Sequence[str] | None) -> dict[str, Any]:
            return self.router.build_image_request(
                self.role,
                [{"role": "user", "content": content}],
                model=model,
                image_config={"aspect_ratio": aspect_ratio},
                catalog=catalogue,
                modalities=modalities,
            )

        first = payload(None)
        try:
            return self._post(first), first["modalities"]
        except _NoEndpointForOutputs:
            if first["modalities"] == IMAGE_ONLY:
                raise
            logger.warning("image model %s has no endpoint for an image and text", model)
        return self._post(payload(IMAGE_ONLY)), list(IMAGE_ONLY)

    def _result(
        self,
        operation: MediaOperation,
        body: dict[str, Any],
        drawn: bytes,
        *,
        asked_for: str,
        width: int,
        height: int,
        params: Mapping[str, Any],
    ) -> MediaResult:
        checked, reason = check_image(RawOutput("image", drawn), self.limits)
        if checked is None:
            logger.warning("image model's image refused: %s", reason)
            raise MediaProviderError("the image the model returned is not an accepted image")
        data, mime, fitted = fit_to_size(checked.data, checked.mime, width, height)
        cost, cost_source = self._cost(body)
        return MediaResult(
            data=data,
            mime=mime,
            width=width,
            height=height,
            cost_usd=cost,
            provider=self.router.config.provider.name,
            model=model_name_to_keep(body.get("model"), asked_for=asked_for),
            params={
                "role": self.role,
                "operation": operation,
                **params,
                "returned_width": checked.width,
                "returned_height": checked.height,
                "fitted": fitted,
                "cost_source": cost_source,
            },
        )

    def _catalogue(self) -> Mapping[str, CatalogModel] | None:
        if self.catalog is None:
            return None
        models = self.catalog()
        if models is None:
            logger.warning(
                "model catalogue not available: the image request asks for an image and text"
            )
        return models

    def _check_size(self, width: int, height: int) -> None:
        for side in (width, height):
            if isinstance(side, bool) or not isinstance(side, int) or side < 1:
                raise ValueError("an image's width and height are whole numbers of pixels")
            if side > self.limits.max_side:
                raise ValueError(f"an image is at most {self.limits.max_side} pixels a side")
        if width * height > self.limits.max_pixels:
            raise ValueError(f"an image has at most {self.limits.max_pixels} pixels")

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = self.router.config.provider.base_url.rstrip("/") + "/chat/completions"
        timeout = self.router.resolve(self.role).timeout_s
        try:
            with httpx.Client(timeout=timeout, transport=self.transport) as client:
                response = client.post(url, json=payload, headers=self.router.headers(self.api_key))
        except httpx.HTTPError as e:
            raise MediaUnavailable(
                f"the image provider could not be reached: {type(e).__name__}"
            ) from e
        if response.status_code != 200:
            logger.warning(
                "image provider answered HTTP %s: %r", response.status_code, response.text[:300]
            )
            if _wants_the_images_endpoint(response):
                raise _ModelNotServed(IMAGES_ENDPOINT_ONLY)
            if _has_no_endpoint_for_the_outputs(response):
                raise _NoEndpointForOutputs(
                    f"the image provider answered HTTP {response.status_code}"
                )
            if response.status_code == _NOT_ON_THIS_ENDPOINT_STATUS:
                raise _ModelNotServed(f"the image provider answered HTTP {response.status_code}")
            raise _failure(
                response.status_code,
                f"the image provider answered HTTP {response.status_code}",
                error=_error_in(response),
            )
        if len(response.content) > MAX_REPLY_BYTES:
            raise MediaProviderError(
                f"the image provider's reply is larger than {MAX_REPLY_BYTES} bytes"
            )
        try:
            body = response.json()
        except ValueError:
            raise MediaProviderError("the image provider's reply is not JSON") from None
        if not isinstance(body, dict):
            raise MediaProviderError("the image provider's reply is not an object")
        if "error" in body or not body.get("choices"):
            error = body.get("error", body)
            logger.warning("image provider reported an error: %r", str(error)[:300])
            raise _failure(
                reported_status(error), "the image provider reported an error", error=error
            )
        return body

    def _image_in(self, body: dict[str, Any]) -> bytes:
        """The bytes of the first image of the reply's first choice."""
        choice = body["choices"][0]
        message = choice.get("message") if isinstance(choice, dict) else None
        if not isinstance(message, dict):
            raise MediaProviderError("the image provider's reply has no message")
        url = _first_image_url(message.get("images")) or _first_image_url(message.get("content"))
        if url is None:
            content = message.get("content")
            logger.warning(
                "image model returned no image (finish %r, native %r): %r",
                choice.get("finish_reason"),
                choice.get("native_finish_reason"),
                content[:300] if isinstance(content, str) else None,
            )
            if _was_refused(choice):
                raise MediaRefused("the image model refused to make this image")
            raise MediaProviderError("the image model returned no image")
        for prefix in _DATA_URL_PREFIXES:
            if url.startswith(prefix):
                try:
                    return base64.b64decode(url.removeprefix(prefix), validate=True)
                except (binascii.Error, ValueError):
                    raise MediaProviderError(
                        "the image the model returned is not readable"
                    ) from None
        raise MediaProviderError("the image the model returned is not a PNG, JPEG or WebP data URL")

    def _cost(self, body: dict[str, Any]) -> tuple[Decimal, str]:
        usage = body.get("usage")
        reported = usage.get("cost") if isinstance(usage, dict) else None
        if isinstance(reported, int | float) and not isinstance(reported, bool):
            if math.isfinite(reported) and reported >= 0:
                try:
                    return Decimal(str(reported)), "provider"
                except InvalidOperation:
                    pass
        estimate = self.router.resolve(self.role).estimate_usd_per_image
        if estimate is None:
            return Decimal(0), "unreported"
        return Decimal(str(estimate)), "estimate"


def nearest_aspect_ratio(width: int, height: int) -> str:
    """The aspect ratio OpenRouter takes that is nearest ``width`` over ``height``."""
    wanted = math.log(width / height)
    return min(ASPECT_RATIOS, key=lambda name: abs(math.log(ASPECT_RATIOS[name]) - wanted))


def fit_to_size(
    data: bytes, mime: ImageMime, width: int, height: int
) -> tuple[bytes, ImageMime, str]:
    """The image at ``width`` by ``height``, its type and what was done: ``none``, ``resized``
    or ``cropped_and_resized``. A fitted image is a PNG; one already at the size is unchanged.
    """
    with Image.open(io.BytesIO(data)) as image:
        returned = image.size
        if returned == (width, height):
            return data, mime, "none"
        same_shape = abs(returned[0] / returned[1] - width / height) <= ASPECT_TOLERANCE * (
            width / height
        )
        has_alpha = "A" in image.getbands() or "transparency" in image.info
        converted = image.convert("RGBA" if has_alpha else "RGB")
    fitted = ImageOps.fit(converted, (width, height), method=Image.Resampling.LANCZOS)
    out = io.BytesIO()
    fitted.save(out, format="PNG")
    return out.getvalue(), "image/png", "resized" if same_shape else "cropped_and_resized"


def _parts(images: Sequence[ImageInput]) -> list[dict[str, Any]]:
    return [image_part(image.data, image.mime) for image in images]


def _first_image_url(parts: Any) -> str | None:
    """The URL of the first image of ``parts``, a list of OpenRouter's image parts:
    ``{"type": "image_url", "image_url": {"url": ...}}``, or with the URL as ``image_url``
    itself. Anything else in the list is passed over."""
    if not isinstance(parts, list):
        return None
    for part in parts:
        image_url = part.get("image_url") if isinstance(part, dict) else None
        if isinstance(image_url, dict):
            image_url = image_url.get("url")
        if isinstance(image_url, str) and image_url:
            return image_url
    return None


def _was_refused(choice: dict[str, Any]) -> bool:
    reasons = (choice.get("finish_reason"), choice.get("native_finish_reason"))
    return any(
        isinstance(reason, str) and any(word in reason.lower() for word in _REFUSAL_REASONS)
        for reason in reasons
    )


def _error_in(response: httpx.Response) -> Any:
    """The ``error`` object of a reply that was not HTTP 200, or ``None``. Read only to tell
    the kind of failure: nothing of it is put in the error raised."""
    try:
        body = response.json()
    except ValueError:
        return None
    return body.get("error") if isinstance(body, dict) else None


class _ModelNotServed(MediaProviderError):
    """OpenRouter serves this model no endpoint for the request (HTTP 404): another may."""


class _NoEndpointForOutputs(_ModelNotServed):
    """The model has no endpoint for the modalities asked: it may answer for an image only."""


def _another_model_may_pass(failure: MediaProviderError) -> bool:
    """Whether the next of the role's models is worth asking after ``failure``: not after a
    refusal of the content or an account that cannot pay, which every model would share."""
    return isinstance(failure, MediaUnavailable | _ModelNotServed)


def _of_all(failures: Sequence[MediaProviderError]) -> MediaProviderError:
    """What is raised when every model failed: one that may pass later, else the first model's,
    as the public error it is (never the module's own kinds)."""
    for failure in failures:
        if isinstance(failure, MediaUnavailable):
            return failure
    return MediaProviderError(str(failures[0]))


def _error_message(response: httpx.Response) -> str:
    error = _error_in(response)
    message = error.get("message") if isinstance(error, dict) else None
    return (message if isinstance(message, str) else response.text[:2000]).lower()


def _has_no_endpoint_for_the_outputs(response: httpx.Response) -> bool:
    """Whether a reply says no endpoint of the model gives the modalities asked. Read only to
    tell the kind of failure: nothing of it is put in the error raised."""
    if response.status_code != _NOT_ON_THIS_ENDPOINT_STATUS:
        return False
    return _OUTPUTS_MARK in _error_message(response)


def _wants_the_images_endpoint(response: httpx.Response) -> bool:
    """Whether a reply says the model is served on the images endpoint, not the chat one. Read
    only to tell the kind of failure: nothing of it is put in the error raised."""
    if response.status_code != _NOT_ON_THIS_ENDPOINT_STATUS:
        return False
    text = _error_message(response)
    return any(mark in text for mark in _IMAGES_ENDPOINT_MARKS)


def _names_a_limit(error: Any) -> bool:
    metadata = error.get("metadata") if isinstance(error, dict) else None
    return isinstance(metadata, dict) and bool(metadata.get("limit_source"))


def _failure(status: int | None, message: str, *, error: Any = None) -> MediaProviderError:
    if status == _ACCOUNT_LIMITED_STATUS:
        return MediaAccountLimited(ACCOUNT_LIMITED)
    if status is not None and (status in _PASSING_STATUSES or status >= 500):
        # A rate limit may name the limit it hit too, and it passes.
        return MediaUnavailable(message)
    if _names_a_limit(error):
        return MediaAccountLimited(ACCOUNT_LIMITED)
    if status is None:
        return MediaUnavailable(message)
    if status in _REFUSED_STATUSES:
        return MediaRefused(message)
    return MediaProviderError(message)
