"""Chat access for agents: one call per model turn, through the model router.

Agents depend on the small ``ChatModel`` protocol, not on a provider. The
default implementation resolves the role with ``ModelRouter`` and posts to the
configured OpenAI-compatible endpoint (OpenRouter by default).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from openmarketer_core.llm_config import DEFAULT_CONFIG_PATH, ModelRouter


class LLMError(Exception):
    """The model call failed or returned something unusable."""


@dataclass(frozen=True)
class ChatReply:
    message: dict[str, Any]  # the assistant message, ready to append to the conversation
    model: str
    cost_usd: float
    prompt_tokens: int = 0
    completion_tokens: int = 0

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
    """``ChatModel`` backed by the per-role model configuration."""

    router: ModelRouter
    transport: httpx.BaseTransport | None = None
    calls: list[ChatReply] = field(default_factory=list)

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
        extra: dict[str, Any] = {"usage": {"include": True}}
        if tool_choice is not None:
            extra["tool_choice"] = tool_choice
        payload = self.router.build_chat_request(role, messages, tools=tools, extra=extra)
        url = self.router.config.provider.base_url.rstrip("/") + "/chat/completions"
        try:
            with httpx.Client(
                timeout=self.router.resolve(role).timeout_s, transport=self.transport
            ) as client:
                response = client.post(url, json=payload, headers=self.router.headers())
        except httpx.HTTPError as e:
            raise LLMError(f"request failed: {type(e).__name__}") from e
        if response.status_code != 200:
            raise LLMError(f"HTTP {response.status_code}: {response.text[:300]}")
        body = response.json()
        if "error" in body or not body.get("choices"):
            raise LLMError(f"provider error: {str(body.get('error', body))[:300]}")

        message = body["choices"][0]["message"]
        usage = body.get("usage") or {}
        reply = ChatReply(
            message={
                key: message[key]
                for key in ("role", "content", "tool_calls")
                if message.get(key) is not None
            },
            model=body.get("model", payload["model"]),
            cost_usd=float(usage.get("cost") or 0.0),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
        )
        self.calls.append(reply)
        return reply
