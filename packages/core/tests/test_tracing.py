"""Tests for tracing agent runs to Langfuse: what is sent, what never is, and when.

Each test that traces starts tracing with an exporter that keeps the spans in memory instead of
sending them, so it reads exactly what would reach Langfuse, and stops it when it ends. A test
has a public key of its own, as Langfuse keeps one client per key.
"""

import ast
import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from openmarketer_core import tracing
from openmarketer_core.agent_runtime.agent import END, Agent
from openmarketer_core.agent_runtime.langgraph_agent import AgentSteps, run_from
from openmarketer_core.agent_runtime.steps import GraphRunRef
from openmarketer_core.agent_runtime.threads import thread_id
from openmarketer_core.graph_checkpoints import RunCheckpoints
from openmarketer_core.llm import ChatModel, ChatReply, LLMError
from openmarketer_core.tracing import (
    LANGFUSE_CLOUD,
    NOT_TRACING,
    Tracing,
    TracingNotConfigured,
    TracingSettings,
    start_tracing,
)

PROVIDER_KEY = "sk-or-v1-made-up-provider-key"
LANGFUSE_SECRET = "sk-lf-made-up-secret"
REPOSITORY_LINE = "export function shareMemory() {}"
KIND = "tracing-test"


@dataclass
class Traced:
    """Tracing as a test started it, and the spans it would have sent to Langfuse."""

    tracing: Tracing
    exporter: InMemorySpanExporter

    def spans(self) -> list[ReadableSpan]:
        self.tracing.flush()
        return list(self.exporter.get_finished_spans())

    def named(self, name: str) -> ReadableSpan:
        return next(span for span in self.spans() if span.name == name)

    def parent_of(self, span: ReadableSpan) -> str | None:
        by_id = {other.context.span_id: other.name for other in self.spans() if other.context}
        return None if span.parent is None else by_id.get(span.parent.span_id)

    def sent(self) -> str:
        """Everything the spans carry, as one text to search."""
        parts: list[str] = []
        for span in self.spans():
            parts.append(span.name)
            parts.extend(str(value) for value in (span.attributes or {}).values())
            for event in span.events:
                parts.extend(str(value) for value in (event.attributes or {}).values())
        return "\n".join(parts)


@pytest.fixture
def traced() -> Iterator[Traced]:
    exporter = InMemorySpanExporter()
    settings = TracingSettings(
        public_key=f"pk-lf-{uuid.uuid4()}",
        secret_key=LANGFUSE_SECRET,
        base_url="http://127.0.0.1:1",
    )
    started = start_tracing(settings, span_exporter=exporter)
    try:
        yield Traced(started, exporter)
    finally:
        started.stop()


class ScriptedModel:
    """A model that answers with a draft, or refuses, and holds a key as the real one does."""

    api_key = PROVIDER_KEY

    def __init__(self, refuses: bool = False) -> None:
        self.refuses = refuses

    def chat(self, role, messages, *, tools=None, tool_choice=None) -> ChatReply:
        if self.refuses:
            raise LLMError("the model provider answered HTTP 402", status=402)
        return ChatReply(
            message={"role": "assistant", "content": "a draft"},
            model="vendor/model-1",
            cost_usd=0.002,
            prompt_tokens=120,
            completion_tokens=30,
        )


@dataclass(frozen=True)
class Context:
    """What the node is given besides the state: the model, and a key, as a run's context is."""

    model: ChatModel
    api_key: str = PROVIDER_KEY


class Notes(TypedDict):
    draft: str


def an_agent() -> Agent:
    agent = Agent(KIND, state=Notes, context=Context)

    @agent.node(start=True)
    def read(state: Notes, context: Context) -> dict[str, Any]:
        reply = context.model.chat("repo_analyzer", [{"role": "user", "content": REPOSITORY_LINE}])
        return {"draft": reply.text}

    agent.edge(read, END)
    return agent


def a_call(model: ChatModel) -> ChatReply:
    return model.chat("repo_analyzer", [{"role": "user", "content": REPOSITORY_LINE}])


def attributes(span: ReadableSpan) -> dict[str, Any]:
    return dict(span.attributes or {})


# ----------------------------------------------------------------- settings


def test_tracing_is_off_when_the_environment_gives_no_keys():
    assert TracingSettings.from_env({}).enabled is False


def test_tracing_is_on_with_both_keys_and_goes_to_langfuse_cloud_unless_told_otherwise():
    settings = TracingSettings.from_env({"LANGFUSE_PUBLIC_KEY": "pk", "LANGFUSE_SECRET_KEY": "sk"})
    assert (settings.enabled, settings.base_url) == (True, LANGFUSE_CLOUD)


@pytest.mark.parametrize("variable", ["LANGFUSE_BASE_URL", "LANGFUSE_HOST"])
def test_self_hosted_langfuse_is_named_by_its_address(variable):
    environ = {"LANGFUSE_PUBLIC_KEY": "pk", "LANGFUSE_SECRET_KEY": "sk", variable: "http://lf"}
    assert TracingSettings.from_env(environ).base_url == "http://lf"


@pytest.mark.parametrize(
    ("given", "missing"),
    [
        ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"),
        ("LANGFUSE_SECRET_KEY", "LANGFUSE_PUBLIC_KEY"),
    ],
)
def test_one_key_without_the_other_is_refused_naming_the_missing_one(given, missing):
    with pytest.raises(TracingNotConfigured, match=missing) as refused:
        TracingSettings.from_env({given: "made-up-key-value"})
    assert "made-up-key-value" not in str(refused.value)


def test_settings_do_not_show_their_keys():
    settings = TracingSettings.from_env(
        {"LANGFUSE_PUBLIC_KEY": "pk-made-up", "LANGFUSE_SECRET_KEY": "sk-made-up"}
    )
    assert "made-up" not in repr(settings)


# ------------------------------------------------------------ tracing off


def test_model_is_given_back_as_it_is_while_tracing_is_off():
    model = ScriptedModel()
    assert NOT_TRACING.model(model) is model
    assert start_tracing(TracingSettings.from_env({})).model(model) is model


# -------------------------------------------------------------- model calls


def test_traced_model_answers_what_the_model_answered(traced):
    reply = a_call(traced.tracing.model(ScriptedModel()))
    assert (reply.text, reply.model, reply.cost_usd) == ("a draft", "vendor/model-1", 0.002)


def test_model_call_is_a_generation_with_its_messages_reply_model_tokens_and_cost(traced):
    a_call(traced.tracing.model(ScriptedModel()))

    call = attributes(traced.named("repo_analyzer"))
    assert call["langfuse.observation.type"] == "generation"
    assert REPOSITORY_LINE in call["langfuse.observation.input"]
    assert "a draft" in call["langfuse.observation.output"]
    assert call["langfuse.observation.model.name"] == "vendor/model-1"
    assert json.loads(call["langfuse.observation.usage_details"]) == {"input": 120, "output": 30}
    assert json.loads(call["langfuse.observation.cost_details"]) == {"total": 0.002}


def test_failed_model_call_is_an_error_with_its_message_and_is_still_raised(traced):
    with pytest.raises(LLMError):
        a_call(traced.tracing.model(ScriptedModel(refuses=True)))

    call = attributes(traced.named("repo_analyzer"))
    assert call["langfuse.observation.level"] == "ERROR"
    assert call["langfuse.observation.status_message"] == "the model provider answered HTTP 402"


def test_keys_are_never_sent(traced):
    a_call(traced.tracing.model(ScriptedModel()))

    assert traced.spans()
    assert PROVIDER_KEY not in traced.sent()
    assert LANGFUSE_SECRET not in traced.sent()


# ------------------------------------------------------------------- graphs


def test_run_in_memory_is_named_after_its_agent_with_its_node_and_the_call_under_it(traced):
    context = Context(model=traced.tracing.model(ScriptedModel()))

    run_from(an_agent(), "read", {"draft": ""}, recursion_limit=5, context=context)

    assert traced.parent_of(traced.named("repo_analyzer")) == "read"
    assert traced.parent_of(traced.named("read")) == KIND
    assert PROVIDER_KEY not in traced.sent()


def test_step_of_a_workflows_run_has_the_runs_thread_as_its_session(traced):
    context = Context(model=traced.tracing.model(ScriptedModel()))
    steps = AgentSteps(
        an_agent(),
        initial_state=lambda run: {"draft": ""},
        finish=lambda run, final: {"draft": final["draft"]},
        recursion_limit=5,
        context=lambda run, checkpoints: context,
    )
    run = GraphRunRef(kind=KIND, run_id=uuid.uuid4())
    thread = thread_id(KIND, run.run_id)

    steps.advance(run, RunCheckpoints(store=InMemorySaver(), thread_id=thread), None)

    assert traced.parent_of(traced.named("repo_analyzer")) == "read"
    assert thread in [str(value) for value in attributes(traced.named(KIND)).values()]
    assert PROVIDER_KEY not in traced.sent()


def test_graphs_are_not_traced_once_tracing_stopped(traced):
    traced.tracing.stop()

    context = Context(model=ScriptedModel())
    run_from(an_agent(), "read", {"draft": ""}, recursion_limit=5, context=context)

    assert traced.exporter.get_finished_spans() == ()


def test_only_the_tracing_module_imports_langfuse():
    core = Path(tracing.__file__).parent
    importing = {
        str(source_file.relative_to(core))
        for source_file in core.rglob("*.py")
        if any(
            name == "langfuse" or name.startswith("langfuse.")
            for name in _imported_names(source_file)
        )
    }
    assert importing == {"tracing.py"}


def _imported_names(source_file: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(source_file.read_text())):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names
