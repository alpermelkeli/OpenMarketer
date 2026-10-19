"""Chat access for agents: one call per model turn, through the model router.

Agents depend on the small ``ChatModel`` protocol, not on a provider. The
default implementation resolves the role with ``ModelRouter`` and posts to the
configured OpenAI-compatible endpoint (OpenRouter by default).

What a provider answers when it refuses a request is not repeated in the error:
an error page can echo request headers or prompt text. The error says the HTTP
status and the response goes to the log.

A role that requires ``vision`` is shown an image as a part of a message's
content (``image_part``), in the OpenAI form OpenRouter reads: the bytes in a
data URL. Only PNG, JPEG and WebP within a size are made into one, and only
when the bytes are what the type says.

A request that forces a tool (``tool_choice`` naming one, or ``"required"``) is refused by
OpenRouter with HTTP 404 when no endpoint of the model takes a forced choice. Such a request
is sent again once without ``tool_choice`` and with one more user message that asks for the
same call; the callers already check which tool the reply called. The model is then
remembered for the life of the process (``ForcedChoiceRefusals``), so its later forced
requests go straight to the second form. Nothing else about the request changes.
"""

from __future__ import annotations

import base64
import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from openmarketer_core.llm_config import DEFAULT_CONFIG_PATH, ModelRouter

logger = logging.getLogger(__name__)

MAX_MODEL_NAME_LENGTH = 200

_PASSING_STATUSES = (408, 429)

# Larger images cost more to send than they tell a model; the studio's canvases are well under it.
MAX_IMAGE_PART_BYTES = 10 * 1024 * 1024

# The first bytes of each image type a model is shown.
_IMAGE_SIGNATURES = {
    "image/png": lambda data: data.startswith(b"\x89PNG\r\n\x1a\n"),
    "image/jpeg": lambda data: data.startswith(b"\xff\xd8\xff"),
    "image/webp": lambda data: len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP",
}


# How many models a process remembers as refusing a forced tool choice; the oldest is forgotten.
MAX_REMEMBERED_REFUSALS = 256


class ForcedChoiceRefusals:
    """The models known, in this process, to refuse a forced ``tool_choice``.

    Bounded and safe across threads; a model forgotten costs one refused request again.
    """

    def __init__(self, limit: int = MAX_REMEMBERED_REFUSALS) -> None:
        self._limit = limit
        self._models: OrderedDict[str, None] = OrderedDict()
        self._lock = threading.Lock()

    def refuses(self, model: str) -> bool:
        with self._lock:
            return model in self._models

    def remember(self, model: str) -> None:
        with self._lock:
            self._models[model] = None
            self._models.move_to_end(model)
            while len(self._models) > self._limit:
                self._models.popitem(last=False)


_REFUSALS_OF_THIS_PROCESS = ForcedChoiceRefusals()


class ImagePartRefused(ValueError):
    """The bytes cannot be shown to a model as an image; the message says why, never the bytes."""


class LLMError(Exception):
    """The model call failed or returned something unusable.

    ``status`` is the HTTP status the provider answered with, or the one it
    reported inside its reply; ``None`` when there was no answer at all.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

    @property
    def may_pass(self) -> bool:
        """Whether the same request could succeed later.

        An unreachable provider, a timeout (408), a rate limit (429) and a
        server error can pass. Any other 4xx says the request itself is refused
        (a wrong key, no credit, an unknown model) and will be refused again.
        """
        if self.status is None:
            return True
        return self.status in _PASSING_STATUSES or self.status >= 500


@dataclass(frozen=True)
class ChatReply:
    message: dict[str, Any]  # the assistant message, ready to append to the conversation
    model: str
    cost_usd: float
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # Why the answer stopped: "stop", "tool_calls", "length" (the answer was cut off by the
    # request's `max_tokens`), or whatever the provider reports. A caller that must tell a
    # cut-off answer from an empty one reads this.
    finish_reason: str | None = None

    @property
    def tool_calls(self) -> list[dict[str, Any]]:
        return self.message.get("tool_calls") or []

    @property
    def text(self) -> str:
        content = self.message.get("content")
        return content if isinstance(content, str) else ""


class ChatModel(Protocol):
    def chat(
        self,
        role: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
    ) -> ChatReply: ...


@dataclass
class RouterChatModel:
    """``ChatModel`` backed by the per-role model configuration.

    ``api_key`` is the provider's key for the calls of this model, for a caller that has
    resolved one itself; without it the key is the one in the router's environment. It is
    left out of ``repr``, which ends up in logs and tracebacks.
    """

    router: ModelRouter
    transport: httpx.BaseTransport | None = None
    calls: list[ChatReply] = field(default_factory=list)
    api_key: str | None = field(default=None, repr=False)
    forced_choice_refusals: ForcedChoiceRefusals = field(
        default_factory=lambda: _REFUSALS_OF_THIS_PROCESS, repr=False
    )

    @classmethod
    def from_env(cls, config_path: str | None = None) -> RouterChatModel:
        import os

        path = config_path or os.environ.get("LLM_CONFIG_PATH") or DEFAULT_CONFIG_PATH
        return cls(router=ModelRouter.from_file(path))

    def chat(
        self,
        role: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
    ) -> ChatReply:
        model_id = self.router.resolve(role).model
        if not forces_a_tool(tool_choice):
            response = self._post(role, messages, tools, tool_choice)
            return self._reply_from(response, asked_for=model_id)
        if not self.forced_choice_refusals.refuses(model_id):
            response = self._post(role, messages, tools, tool_choice)
            if not refuses_forced_tool_choice(response):
                return self._reply_from(response, asked_for=model_id)
            self.forced_choice_refusals.remember(model_id)
            logger.info(
                "model %s does not take a forced tool choice; "
                "the request was sent again without it",
                model_id,
            )
        unforced = self._post(role, asked_without_forcing(messages, tool_choice), tools, None)
        return self._reply_from(unforced, asked_for=model_id)

    def _post(
        self,
        role: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        tool_choice: Any,
    ) -> httpx.Response:
        extra: dict[str, Any] = {"usage": {"include": True}}
        if tool_choice is not None:
            extra["tool_choice"] = tool_choice
        payload = self.router.build_chat_request(role, messages, tools=tools, extra=extra)
        url = self.router.config.provider.base_url.rstrip("/") + "/chat/completions"
        try:
            with httpx.Client(
                timeout=self.router.resolve(role).timeout_s, transport=self.transport
            ) as client:
                return client.post(url, json=payload, headers=self.router.headers(self.api_key))
        except httpx.HTTPError as e:
            raise LLMError(f"request failed: {type(e).__name__}") from e

    def _reply_from(self, response: httpx.Response, *, asked_for: str) -> ChatReply:
        if response.status_code != 200:
            logger.warning(
                "model provider answered HTTP %s: %r", response.status_code, response.text[:300]
            )
            raise LLMError(
                f"the model provider answered HTTP {response.status_code}",
                status=response.status_code,
            )
        body = response.json()
        if "error" in body or not body.get("choices"):
            error = body.get("error", body)
            logger.warning("model provider reported an error: %r", str(error)[:300])
            raise LLMError("the model provider reported an error", status=reported_status(error))

        choice = body["choices"][0]
        message = choice["message"]
        usage = body.get("usage") or {}
        finish_reason = choice.get("finish_reason")
        reply = ChatReply(
            message={
                key: message[key]
                for key in ("role", "content", "tool_calls")
                if message.get(key) is not None
            },
            model=model_name_to_keep(body.get("model"), asked_for=asked_for),
            cost_usd=float(usage.get("cost") or 0.0),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            finish_reason=finish_reason if isinstance(finish_reason, str) else None,
        )
        self.calls.append(reply)
        return reply


def forces_a_tool(tool_choice: Any) -> bool:
    """Whether ``tool_choice`` makes the model call a tool: one it names, or ``"required"``."""
    if tool_choice == "required":
        return True
    if not isinstance(tool_choice, dict) or tool_choice.get("type") != "function":
        return False
    function = tool_choice.get("function")
    return isinstance(function, dict) and isinstance(function.get("name"), str)


def refuses_forced_tool_choice(response: httpx.Response) -> bool:
    """Whether the provider refused the request only because it forced a tool choice.

    OpenRouter answers HTTP 404 with "No endpoints found that support the provided
    'tool_choice' value" when no endpoint of the model takes one. Anything else, a 404 for an
    unknown model among them, is not this refusal.
    """
    if response.status_code != 404:
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    error = body.get("error") if isinstance(body, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    if not isinstance(message, str):
        return False
    words = message.lower()
    return "no endpoints found" in words and "tool_choice" in words


def asked_without_forcing(messages: list[dict[str, Any]], tool_choice: Any) -> list[dict[str, Any]]:
    """``messages`` with the instruction that asks in words for the call ``tool_choice`` forced."""
    if tool_choice == "required":
        instruction = (
            "Answer now by calling one of the tools with your final answer; do not answer in text."
        )
    else:
        name = tool_choice["function"]["name"]
        instruction = (
            f"Answer now by calling the tool `{name}` with your final answer; "
            "do not call any other tool and do not answer in text."
        )
    return [*messages, {"role": "user", "content": instruction}]


def model_name_to_keep(reported: Any, *, asked_for: str) -> str:
    """The model the provider says answered, or the one asked for when that cannot be kept.

    The provider writes the name, and it is stored with the run: in its
    checkpoints and its records. PostgreSQL stores no NUL character, so one in
    the name would fail every write of the run. A name is kept only when it is
    printable text of a plausible length.
    """
    if isinstance(reported, str) and reported.isprintable():
        if 0 < len(reported) <= MAX_MODEL_NAME_LENGTH:
            return reported
    return asked_for


def reported_status(error: Any) -> int | None:
    """The status a provider puts in the error of a reply it sent with HTTP 200."""
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, int) and not isinstance(code, bool) else None


def image_part(data: bytes, mime: str) -> dict[str, Any]:
    """``data`` as a part of a message's content that shows a model the image.

    Raises ``ImagePartRefused`` for a type other than PNG, JPEG or WebP, for bytes that do not
    start as that type does, and for an empty image or one over ``MAX_IMAGE_PART_BYTES``.
    """
    matches = _IMAGE_SIGNATURES.get(mime)
    if matches is None:
        raise ImagePartRefused("an image is shown as PNG, JPEG or WebP")
    if not data:
        raise ImagePartRefused("the image is empty")
    if len(data) > MAX_IMAGE_PART_BYTES:
        raise ImagePartRefused(f"an image shown to a model is at most {MAX_IMAGE_PART_BYTES} bytes")
    if not matches(data):
        raise ImagePartRefused(f"the bytes are not a {mime} image")
    encoded = base64.b64encode(data).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}
