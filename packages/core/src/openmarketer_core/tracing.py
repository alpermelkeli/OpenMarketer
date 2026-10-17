"""Tracing agent runs to Langfuse: what a graph's nodes did, and every model call they made.

Tracing is off unless the environment gives a Langfuse project's keys:
``LANGFUSE_PUBLIC_KEY`` and ``LANGFUSE_SECRET_KEY``, and ``LANGFUSE_BASE_URL`` for a
Langfuse other than Langfuse Cloud (a self-hosted one; ``LANGFUSE_HOST`` is read as well).
``start_tracing`` reads those settings once, when a process starts, and gives the process its
``Tracing``; ``Tracing.stop`` sends what is still queued and ends it.

Langfuse is built on OpenTelemetry: each traced thing is a span, exported to Langfuse in
batches. Two kinds are made here:

- **A graph run and its nodes.** Langfuse's LangChain handler is attached to every LangChain
  run of the process through LangChain's configure hook (a context variable this module
  sets), so LangGraph reports each node, with the state it read and what it changed, and the
  graph adapters need no import of Langfuse. They name a run after its kind and give a step of
  a workflow's run its thread as its Langfuse session (``langgraph_steps.py``), so the steps of
  one run are one session. Work that may not descend from the context ``start_tracing`` ran in
  is run under ``Tracing.traced`` instead (the API's chat turns), which sets the same handler
  for the work's own context.
- **A model call.** Ours go through ``llm.py`` and not through LangChain, so LangGraph cannot
  see them. ``TracedChatModel`` records each as a *generation* under the node that made it:
  its role, the messages and tools it was sent, the message it got back, the model, its
  tokens and its cost.
- **An image call.** ``studio/openrouter_images.py`` posts outside LangChain as well.
  ``TracedMediaProvider`` records each as a generation: its operation, the prompt or the
  instruction, the size asked for and how many images were sent with it (not their bytes),
  what was asked of the model and done to its answer, the model and the cost. No image's bytes
  are sent to Langfuse.
- **A decision call.** ``decisions.py`` posts to its own endpoint, not through LangChain
  either. ``TracedDecisionModel`` records each as a generation the same way: its role, the
  texts and the questions it was sent, the probability of each answer, the model and its
  cost (the endpoint reports no tokens).

What leaves the process, when tracing is on: the graph's state and every prompt and reply,
which hold repository content (after intake blanked out the secrets it found), and the texts
and answers of every decision call (a research run's questions and search results), sent to
Langfuse Cloud unless ``LANGFUSE_BASE_URL`` names a self-hosted Langfuse. That is why it is off
by default. What does not: the model provider's key, which is in no call's inputs (they are
built here, never taken from the wrapped model) and in no graph's state; a run's context is
given to the nodes and not traced. Langfuse's keys are the operator's, read from the
environment when the process starts, not a workspace's: they are not among the credentials
the dashboard stores.

The spans are made with a tracer provider of this module's own, never the process's global
one. This is the only module that imports ``langfuse``; it decides nothing about a run.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.tracers.context import register_configure_hook
from langfuse import Langfuse
from langfuse.langchain import CallbackHandler
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter

from openmarketer_core.decision_questions import YesNoQuestion
from openmarketer_core.decisions import DecisionModel, DecisionReply
from openmarketer_core.llm import ChatModel, ChatReply, LLMError
from openmarketer_core.studio.media_provider import (
    ImageInput,
    MediaOperation,
    MediaProvider,
    MediaProviderError,
    MediaResult,
)

logger = logging.getLogger(__name__)

LANGFUSE_CLOUD = "https://cloud.langfuse.com"

# The handler LangChain adds to every run while it is set: LangGraph's runs and their nodes.
_GRAPH_HANDLER: ContextVar[BaseCallbackHandler | None] = ContextVar(
    "openmarketer_graph_tracing", default=None
)
register_configure_hook(_GRAPH_HANDLER, inheritable=True)


class TracingNotConfigured(Exception):
    """Tracing is half set up; the message says what to set, and never a value."""


@dataclass(frozen=True)
class TracingSettings:
    """Whether runs are traced, and where. Read from the environment.

    The keys are kept out of ``repr``, which ends up in logs and tracebacks.
    """

    public_key: str
    secret_key: str
    base_url: str

    @property
    def enabled(self) -> bool:
        return bool(self.public_key)

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> TracingSettings:
        """The settings in ``environ``: on with both keys, off with neither.

        Raises ``TracingNotConfigured`` for one key without the other, instead of losing every
        trace to a refusal nobody reads.
        """
        public_key = environ.get("LANGFUSE_PUBLIC_KEY", "").strip()
        secret_key = environ.get("LANGFUSE_SECRET_KEY", "").strip()
        if bool(public_key) != bool(secret_key):
            missing = "LANGFUSE_SECRET_KEY" if public_key else "LANGFUSE_PUBLIC_KEY"
            raise TracingNotConfigured(
                f"tracing to Langfuse needs both keys and {missing} is not set: set it, "
                "or remove the other to trace nothing"
            )
        base_url = (
            environ.get("LANGFUSE_BASE_URL", "").strip()
            or environ.get("LANGFUSE_HOST", "").strip()
            or LANGFUSE_CLOUD
        )
        return cls(public_key=public_key, secret_key=secret_key, base_url=base_url)

    def describe(self) -> str:
        """What happens to traces, for the log of a process that starts."""
        if not self.enabled:
            return "runs are not traced (LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are not set)"
        return f"runs are traced to Langfuse at {self.base_url}"

    def __repr__(self) -> str:
        return f"TracingSettings(enabled={self.enabled}, base_url={self.base_url!r})"


class Tracing:
    """The tracing of one process: off, or a Langfuse client and the handler of its graphs."""

    def __init__(
        self,
        client: Langfuse | None,
        token: Token[BaseCallbackHandler | None] | None,
        handler: BaseCallbackHandler | None = None,
    ):
        self._client = client
        self._token = token
        self._handler = handler

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def model(self, inner: ChatModel) -> ChatModel:
        """``inner``, with its calls traced when tracing is on; ``inner`` itself when it is off."""
        if self._client is None:
            return inner
        return TracedChatModel(inner, self._client)

    def decisions(self, inner: DecisionModel) -> DecisionModel:
        """``inner``, with its calls traced when tracing is on; ``inner`` itself when it is off."""
        if self._client is None:
            return inner
        return TracedDecisionModel(inner, self._client)

    def media(self, inner: MediaProvider) -> MediaProvider:
        """``inner``, with its calls traced when tracing is on; ``inner`` itself when it is off."""
        if self._client is None:
            return inner
        return TracedMediaProvider(inner, self._client)

    @contextmanager
    def traced(self) -> Iterator[None]:
        """Run the graphs of this context traced, whatever context this runs in.

        ``start_tracing`` sets the handler in the context it runs in and what descends from
        it; a caller that cannot rely on descending (the API runs a chat turn in a worker
        thread of its server) runs the work under this instead. Nothing when tracing is off.
        """
        if self._handler is None:
            yield
            return
        token = _GRAPH_HANDLER.set(self._handler)
        try:
            yield
        finally:
            _GRAPH_HANDLER.reset(token)

    def flush(self) -> None:
        """Send what is queued; a failure to send is logged and never raised."""
        if self._client is None:
            return
        try:
            self._client.flush()
        except Exception:  # noqa: BLE001  (traces must never fail the work they watch)
            logger.warning("traces still queued were not sent to Langfuse", exc_info=True)

    def stop(self) -> None:
        """Send what is queued and stop tracing graphs; for a process, or a test, that ends."""
        self.flush()
        if self._token is not None:
            _GRAPH_HANDLER.reset(self._token)
            self._token = None
        if self._client is not None:
            self._client.shutdown()
            self._client = None


NOT_TRACING = Tracing(client=None, token=None)


def start_tracing(
    settings: TracingSettings, *, span_exporter: SpanExporter | None = None
) -> Tracing:
    """Start tracing as ``settings`` say, for the graphs run in this context and those it starts.

    The graph handler is set in the current context: a thread or a task started from it
    afterwards traces too. ``span_exporter`` replaces the export to Langfuse, for a test that
    reads what would be sent.
    """
    if not settings.enabled:
        return NOT_TRACING
    client = Langfuse(
        public_key=settings.public_key,
        secret_key=settings.secret_key,
        base_url=settings.base_url,
        tracer_provider=TracerProvider(),
        span_exporter=span_exporter,
    )
    handler = CallbackHandler(public_key=settings.public_key)
    token = _GRAPH_HANDLER.set(handler)
    return Tracing(client, token, handler)


@dataclass(frozen=True)
class TracedChatModel:
    """A ``ChatModel`` whose every call is a Langfuse generation."""

    inner: ChatModel
    client: Langfuse

    def chat(
        self,
        role: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
    ) -> ChatReply:
        call: dict[str, Any] = {"messages": messages}
        if tools is not None:
            call["tools"] = tools
        if tool_choice is not None:
            call["tool_choice"] = tool_choice
        with self.client.start_as_current_observation(
            as_type="generation", name=role, input=call, metadata={"role": role}
        ) as generation:
            try:
                reply = self.inner.chat(role, messages, tools=tools, tool_choice=tool_choice)
            except LLMError as e:
                # Its message says the status and never what the provider wrote.
                generation.update(level="ERROR", status_message=str(e))
                raise
            generation.update(
                output=reply.message,
                model=reply.model,
                usage_details={"input": reply.prompt_tokens, "output": reply.completion_tokens},
                cost_details={"total": reply.cost_usd},
            )
            return reply


@dataclass(frozen=True)
class TracedDecisionModel:
    """A ``DecisionModel`` whose every call is a Langfuse generation."""

    inner: DecisionModel
    client: Langfuse

    def decide(
        self, role: str, state: Sequence[str], questions: Mapping[str, YesNoQuestion]
    ) -> DecisionReply:
        call = {
            "state": list(state),
            "questions": {key: question.as_request() for key, question in questions.items()},
        }
        with self.client.start_as_current_observation(
            as_type="generation", name=role, input=call, metadata={"role": role}
        ) as generation:
            try:
                reply = self.inner.decide(role, state, questions)
            except LLMError as e:
                # A ``DecisionError``: its message says the status and never what was written.
                generation.update(level="ERROR", status_message=str(e))
                raise
            generation.update(
                output=dict(reply.answers),
                model=reply.model,
                cost_details={"total": reply.cost_usd},
            )
            return reply


@dataclass(frozen=True)
class TracedMediaProvider:
    """A ``MediaProvider`` whose every image is a Langfuse generation, without its bytes."""

    inner: MediaProvider
    client: Langfuse

    def generate(
        self, prompt: str, *, width: int, height: int, references: Sequence[ImageInput] = ()
    ) -> MediaResult:
        call = {"prompt": prompt, "width": width, "height": height, "references": len(references)}
        return self._traced(
            "generate",
            call,
            lambda: self.inner.generate(prompt, width=width, height=height, references=references),
        )

    def edit(
        self,
        image: ImageInput,
        instruction: str,
        *,
        width: int,
        height: int,
        references: Sequence[ImageInput] = (),
    ) -> MediaResult:
        call = {
            "instruction": instruction,
            "width": width,
            "height": height,
            "references": len(references),
        }
        return self._traced(
            "edit",
            call,
            lambda: self.inner.edit(
                image, instruction, width=width, height=height, references=references
            ),
        )

    def estimate(self, operation: MediaOperation) -> Decimal:
        return self.inner.estimate(operation)

    def _traced(
        self, operation: MediaOperation, call: dict[str, Any], make: Callable[[], MediaResult]
    ) -> MediaResult:
        with self.client.start_as_current_observation(
            as_type="generation", name=f"image.{operation}", input=call
        ) as generation:
            try:
                result = make()
            except MediaProviderError as e:
                # A fixed sentence: never what the provider wrote.
                generation.update(level="ERROR", status_message=str(e))
                raise
            generation.update(
                output={"mime": result.mime, "params": dict(result.params)},
                model=result.model,
                cost_details={"total": float(result.cost_usd)},
            )
            return result
