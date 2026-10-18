"""Tests for the creative studio's graph, run step by step with its actions answered here.

The three roles are scripted (``ScriptedChat``), the actions' handlers are a fake world that
stores small PNGs and reads back the text a test gives (``World``), and the thread is kept in
memory by the strict serializer the PostgreSQL store uses, so a state that is not plain data
fails here as it would in the worker. Nothing reaches a network or a database.
"""

import io
import json
import uuid
from collections.abc import Callable, Mapping
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from PIL import Image

from openmarketer_core.agent_runtime.actions import ActionOutcome
from openmarketer_core.agent_runtime.langgraph_agent import AgentSteps
from openmarketer_core.agent_runtime.steps import GraphRunRef
from openmarketer_core.agent_runtime.threads import thread_id
from openmarketer_core.graph_checkpoints import RunCheckpoints
from openmarketer_core.llm import ChatReply
from openmarketer_core.run_events import RunEvent
from openmarketer_core.studio.brand_kit import EMPTY_BRAND_KIT, BrandKit
from openmarketer_core.studio.failures import StudioError
from openmarketer_core.studio.media_provider import ImageInput
from openmarketer_core.studio.platforms import Platform
from openmarketer_core.studio.requests import AssetRequest
from openmarketer_core.studio.studio_agent import rules
from openmarketer_core.studio.studio_agent.definitions import (
    EDIT_IMAGE,
    GENERATE_IMAGE,
    READ_TEXT,
    RENDER_HTML,
    RUN_PYTHON,
    StudioContext,
    agent,
    initial_state,
    result_of,
)
from openmarketer_core.studio.studio_agent.graph import _payload_of, recursion_limit
from openmarketer_core.studio.studio_agent.rules import Limits
from openmarketer_core.studio.workflow_contract import STUDIO_RUN_KIND, StudioRunInput

COPY = ["Launch day", "Try it free"]
READ_BACK = "LAUNCH DAY\nTry it free"
ASSET_ID = "99999999-9999-4999-8999-999999999999"

Answer = ChatReply | Callable[[list[dict[str, Any]]], ChatReply]


# ------------------------------------------------------------------------ the scripted models
class ScriptedChat:
    """Prepared answers per role, in order; each call records what it was sent."""

    def __init__(self, **answers: list[Answer]):
        self.answers = {role: list(queue) for role, queue in answers.items()}
        self.calls: list[dict[str, Any]] = []

    def chat(self, role, messages, *, tools=None, tool_choice=None) -> ChatReply:
        self.calls.append({"role": role, "messages": messages, "tools": tools})
        answer = self.answers[role].pop(0)
        return answer(messages) if callable(answer) else answer

    def left(self) -> dict[str, int]:
        return {role: len(queue) for role, queue in self.answers.items() if queue}


def text(content: dict[str, Any]) -> ChatReply:
    return ChatReply(
        message={"role": "assistant", "content": json.dumps(content)}, model="m", cost_usd=0.0
    )


def call(name: str, arguments: dict[str, Any]) -> ChatReply:
    tool_call = {
        "id": f"call-{uuid.uuid4().hex[:6]}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }
    return ChatReply(
        message={"role": "assistant", "tool_calls": [tool_call]}, model="m", cost_usd=0.0
    )


def a_brief(
    technique: str = "generated", image_prompt: str = "A bright desk at dawn, top view."
) -> ChatReply:
    return text(
        {
            "goal": "Announce the launch.",
            "visual_concept": "A bright desk at dawn.",
            "technique": technique,
            "image_prompt": image_prompt,
            "copy_placement": "Centered, upper third.",
        }
    )


def operation(op: str, **fields: Any) -> ChatReply:
    return call(rules.SUBMIT_OPERATION, {"op": op, **fields})


def images_shown(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The run's images, as code told the model of them in the first user message."""
    return json.loads(messages[1]["content"][0]["text"])["images"]


def latest_image(messages: list[dict[str, Any]]) -> str:
    return images_shown(messages)[-1]["version_id"]


def ready() -> Answer:
    """The model names the latest image of the run as the round's candidate."""
    return lambda messages: operation("candidate_ready", version_id=latest_image(messages))


def editing_latest(**fields: Any) -> Answer:
    return lambda messages: operation(
        "edit_image", source_version_id=latest_image(messages), **fields
    )


def a_plan() -> Answer:
    return lambda messages: call(
        rules.SUBMIT_EDIT_PLAN,
        {
            "summary": "Fix the words.",
            "steps": ["Edit the headline."],
            "source_version_id": latest_image(messages),
        },
    )


def a_verdict(**failing: str) -> ChatReply:
    return text(
        {
            "criteria": [
                {
                    "criterion": name,
                    "passed": name not in failing,
                    "severity": failing.get(name, "none"),
                    "note": "",
                }
                for name in rules.CRITERIA
            ],
            "edit_suggestions": [],
        }
    )


ACME = BrandKit(name="Acme", palette=("#112233",))


# ------------------------------------------------------------------------------- the world
def a_png(width: int, height: int) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), (240, 200, 80)).save(out, format="PNG")
    return out.getvalue()


class World:
    """The actions' handlers: images stored by version id, text read back in the given order."""

    def __init__(self, read_back: list[str], *, cost_usd: float = 0.04, sizes=None, failing=None):
        self.read_back = list(read_back)
        # an action's name -> the errors of its next asks, in order
        self.failing: dict[str, list[str]] = {
            name: list(errors) for name, errors in (failing or {}).items()
        }
        self.cost_usd = cost_usd
        self.sizes = dict(sizes or {})  # an image action's position -> (width, height)
        self.images: dict[str, bytes] = {}
        self.asked: list[tuple[str, dict[str, Any]]] = []

    def answer(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.asked.append((name, payload))
        if name == READ_TEXT:
            return {"text": self.read_back.pop(0)}
        made = sum(1 for asked, _ in self.asked if asked != READ_TEXT) - 1
        width, height = self.sizes.get(made, (1080, 1080))
        data = a_png(width, height)
        self.images[payload["version_id"]] = data
        spends = name in (GENERATE_IMAGE, EDIT_IMAGE)
        return {
            "version_id": payload["version_id"],
            "asset_id": ASSET_ID,
            "width": width,
            "height": height,
            "mime": "image/png",
            "size_bytes": len(data),
            "cost_usd": self.cost_usd if spends else 0.0,
        }

    def image_of(self, version_id: str) -> ImageInput:
        return ImageInput(self.images[version_id], "image/png")

    def names(self) -> list[str]:
        return [name for name, _ in self.asked]

    def payloads(self, name: str) -> list[dict[str, Any]]:
        return [payload for asked, payload in self.asked if asked == name]


class Run:
    """One run of the graph to its end, its actions answered by ``world``."""

    def __init__(
        self,
        chat: ScriptedChat,
        world: World,
        *,
        limits: Limits | None = None,
        brand_kit: BrandKit = ACME,
    ):
        request = AssetRequest(
            goal="Announce the launch.", exact_copy=COPY, platform=Platform.INSTAGRAM_POST
        )
        run = StudioRunInput(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
        context = StudioContext(
            model=chat,
            limits=limits or Limits(),
            image_of=world.image_of,
            brand_kit=brand_kit,
        )
        self.events: list[RunEvent] = []
        finals: list[dict[str, Any]] = []

        def finish(ref: GraphRunRef, final: Mapping[str, Any]) -> dict[str, Any]:
            finals.append(dict(final))
            return {}

        steps = AgentSteps(
            agent,
            initial_state=lambda ref: initial_state(run, request),
            finish=finish,
            context=lambda ref, checkpoints: context,
            recursion_limit=recursion_limit(context.limits),
            record_progress=lambda ref, events: self.events.extend(events),
        )
        ref = GraphRunRef(STUDIO_RUN_KIND, run.run_id)
        store = InMemorySaver(serde=JsonPlusSerializer(allowed_msgpack_modules=None))
        checkpoints = RunCheckpoints(store=store, thread_id=thread_id(ref.kind, ref.run_id))
        resume = None
        while True:
            step = steps.advance(ref, checkpoints, resume)
            if step.action is None:
                break
            errors = world.failing.get(step.action.name)
            if errors:
                world.asked.append((step.action.name, step.action.payload))
                resume = ActionOutcome(step.action.action_id, False, {}, errors.pop(0))
                continue
            answer = world.answer(step.action.name, step.action.payload)
            resume = ActionOutcome(step.action.action_id, True, answer, "")
        [self.final] = finals
        self.result = result_of(self.final)

    def kinds(self) -> list[str]:
        return [event.kind for event in self.events]


# ------------------------------------------------------------------------------- the runs
def test_a_generated_image_whose_words_read_back_passes_in_one_round():
    chat = ScriptedChat(
        creative_director=[a_brief()],
        studio=[operation("generate_image", prompt="A bright desk at dawn."), ready()],
        asset_verifier=[a_verdict()],
    )
    world = World([READ_BACK])

    run = Run(chat, world)

    assert world.names() == [GENERATE_IMAGE, READ_TEXT]
    [generated] = world.payloads(GENERATE_IMAGE)
    assert run.result.final_version_id == generated["version_id"]
    assert not run.result.needs_review
    assert run.result.rounds == 1
    assert '"Launch day"\n"Try it free"' in generated["prompt"]
    assert chat.left() == {}
    assert run.kinds() == ["briefed", "produced", "checked", "verified", "finalized"]


A_KIT = BrandKit(
    name="Acme",
    kind="mobile app on ios",
    audience="students",
    features=("Syncs notes.",),
    do_not_mention=("Rival",),
)


def test_a_brief_for_a_background_with_copy_does_not_forbid_the_words_it_asks_for():
    banned = "A navy gradient, no text, no letters, no numbers, no logos."
    chat = ScriptedChat(
        creative_director=[a_brief("background_overlay", image_prompt=banned)],
        studio=[operation("generate_image", prompt=banned), ready()],
        asset_verifier=[a_verdict()],
    )
    world = World([READ_BACK])

    Run(chat, world)

    [generated] = world.payloads(GENERATE_IMAGE)
    assert generated["technique"] == "generated"
    assert generated["prompt"].startswith("A navy gradient, no logos.")
    assert "no letters" not in generated["prompt"]
    assert '"Launch day"\n"Try it free"' in generated["prompt"]
    assert "Where the words go: Centered, upper third." in generated["prompt"]


def test_every_generation_and_edit_is_told_what_the_product_is():
    chat = ScriptedChat(
        creative_director=[a_brief()],
        studio=[
            operation("generate_image", prompt="A desk."),
            ready(),
            a_plan(),
            editing_latest(instruction="Fix it."),
            ready(),
        ],
        asset_verifier=[a_verdict()],
    )
    world = World(["Lauch day", READ_BACK])

    Run(chat, world, brand_kit=A_KIT)

    [generated] = world.payloads(GENERATE_IMAGE)
    [edited] = world.payloads(EDIT_IMAGE)
    about = rules.about_product(A_KIT, COPY)
    assert about in generated["prompt"]
    assert about in edited["instruction"]


def test_an_empty_brand_kit_adds_no_product_block():
    chat = ScriptedChat(
        creative_director=[a_brief()],
        studio=[operation("generate_image", prompt="A desk."), ready()],
        asset_verifier=[a_verdict()],
    )
    world = World([READ_BACK])

    Run(chat, world, brand_kit=EMPTY_BRAND_KIT)

    [generated] = world.payloads(GENERATE_IMAGE)
    assert "About the product" not in generated["prompt"]


def test_the_creative_director_and_the_studio_are_shown_the_brand_kit():
    chat = ScriptedChat(
        creative_director=[a_brief()],
        studio=[operation("generate_image", prompt="A desk."), ready()],
        asset_verifier=[a_verdict()],
    )

    Run(chat, World([READ_BACK]), brand_kit=A_KIT)

    director, studio = chat.calls[0], chat.calls[1]
    assert json.loads(director["messages"][1]["content"])["brand_kit"] == A_KIT.as_data()
    shown = json.loads(studio["messages"][1]["content"][0]["text"])
    assert shown["brand_kit"] == A_KIT.as_data()


def test_a_thread_stored_before_the_product_block_still_asks_for_its_image():
    run = StudioRunInput(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    request = AssetRequest(goal="Launch.", exact_copy=COPY, platform=Platform.INSTAGRAM_POST)
    state: dict[str, Any] = {**initial_state(run, request), "round": 1}
    del state["about_product"]
    pending = {
        "op": GENERATE_IMAGE,
        "version_id": str(uuid.uuid4()),
        "technique": "generated",
        "prompt": "A desk.",
        "reference_version_ids": [],
        "reference_asset_ids": [],
    }

    payload = _payload_of(state, pending)  # type: ignore[arg-type]

    assert "About the product" not in payload.model_dump()["prompt"]


def test_words_read_back_wrong_are_edited_in_a_second_round_that_passes():
    chat = ScriptedChat(
        creative_director=[a_brief()],
        studio=[
            operation("generate_image", prompt="A desk."),
            ready(),
            a_plan(),
            editing_latest(instruction="Spell the headline right."),
            ready(),
        ],
        asset_verifier=[a_verdict()],
    )
    world = World(["Lauch day\nTry it free", READ_BACK])

    run = Run(chat, world)

    assert world.names() == [GENERATE_IMAGE, READ_TEXT, EDIT_IMAGE, READ_TEXT]
    [generated] = world.payloads(GENERATE_IMAGE)
    [edited] = world.payloads(EDIT_IMAGE)
    assert edited["source_version_id"] == generated["version_id"]
    assert run.result.final_version_id == edited["version_id"]
    assert run.result.rounds == 2
    [first, second] = run.result.reviews
    assert first.check_report["copy"]["misspelled"] == [
        {"expected": "Launch day", "read": "lauch day"}
    ]
    assert first.verifier_report is None and not first.passed
    assert second.passed
    assert "edit_planned" in run.kinds()


def test_the_same_wrong_words_twice_switch_to_a_background_with_the_words_in_code():
    chat = ScriptedChat(
        creative_director=[a_brief()],
        studio=[
            operation("generate_image", prompt="A desk."),
            ready(),
            a_plan(),
            editing_latest(instruction="Spell it right."),
            ready(),
            a_plan(),
            operation("generate_image", prompt="A desk, empty space at the top."),
            lambda messages: operation(
                "run_python",
                code="from PIL import Image\n",
                input_version_ids=[latest_image(messages)],
            ),
            ready(),
        ],
        asset_verifier=[a_verdict()],
    )
    world = World(["Lauch day\nTry it free", "Lanch day\nTry it free", READ_BACK])

    run = Run(chat, world)

    assert run.final["technique"] == "background_overlay"
    background = world.payloads(GENERATE_IMAGE)[-1]
    assert background["technique"] == "background_overlay"
    assert "Launch day" not in background["prompt"]
    [program] = world.payloads(RUN_PYTHON)
    assert program["input_version_ids"] == [background["version_id"]]
    assert run.result.final_version_id == program["version_id"]
    [switched] = [event for event in run.events if event.kind == "technique_switched"]
    assert switched.detail == {
        "from_technique": "generated",
        "to_technique": "background_overlay",
        "round": 2,
    }


def test_a_run_at_its_round_limit_escalates_with_its_best_candidate():
    chat = ScriptedChat(
        creative_director=[a_brief()],
        studio=[
            operation("generate_image", prompt="A desk."),
            ready(),
            a_plan(),
            editing_latest(instruction="Recompose."),
            ready(),
        ],
        asset_verifier=[a_verdict(composition="major")],
    )
    # The edit comes back at the wrong size and with its words wrong: two failures to one.
    world = World([READ_BACK, "Lauch day"], sizes={1: (1080, 1000)})

    run = Run(chat, world, limits=Limits(max_rounds=2))

    [generated] = world.payloads(GENERATE_IMAGE)
    assert run.result.needs_review
    assert run.result.final_version_id == generated["version_id"]
    assert "round limit" in run.result.reason
    assert run.kinds()[-1] == "escalated"


def test_an_html_reference_renders_the_layout_and_generates_from_it():
    chat = ScriptedChat(
        creative_director=[a_brief("html_reference")],
        studio=[
            operation("render_html", html="<html><body><h1>Launch day</h1></body></html>"),
            lambda messages: operation(
                "generate_image",
                prompt="A polished version of this layout.",
                reference_version_ids=[latest_image(messages)],
            ),
            ready(),
        ],
        asset_verifier=[a_verdict()],
    )
    world = World([READ_BACK])

    run = Run(chat, world)

    assert world.names() == [RENDER_HTML, GENERATE_IMAGE, READ_TEXT]
    [layout] = world.payloads(RENDER_HTML)
    [generated] = world.payloads(GENERATE_IMAGE)
    assert generated["reference_version_ids"] == [layout["version_id"]]
    assert generated["technique"] == "html_reference"
    assert run.result.final_version_id == generated["version_id"]


def test_references_a_weak_model_sends_as_json_text_are_used():
    chat = ScriptedChat(
        creative_director=[a_brief("html_reference")],
        studio=[
            operation("render_html", html="<html></html>"),
            lambda messages: operation(
                "generate_image",
                prompt="From the layout.",
                reference_version_ids=json.dumps([latest_image(messages)]),
            ),
            ready(),
        ],
        asset_verifier=[a_verdict()],
    )
    world = World([READ_BACK])

    Run(chat, world)

    [layout] = world.payloads(RENDER_HTML)
    [generated] = world.payloads(GENERATE_IMAGE)
    assert generated["reference_version_ids"] == [layout["version_id"]]


def test_the_verifier_is_shown_the_candidate_as_an_image():
    chat = ScriptedChat(
        creative_director=[a_brief()],
        studio=[operation("generate_image", prompt="A desk."), ready()],
        asset_verifier=[a_verdict()],
    )

    Run(chat, World([READ_BACK]))

    [asked] = [c for c in chat.calls if c["role"] == rules.VERIFIER_ROLE]
    assert asked["tools"] is None
    parts = asked["messages"][1]["content"]
    [image] = [part for part in parts if part["type"] == "image_url"]
    assert image["image_url"]["url"].startswith("data:image/png;base64,")


def test_no_payload_of_an_action_carries_an_image():
    chat = ScriptedChat(
        creative_director=[a_brief()],
        studio=[
            operation("generate_image", prompt="A desk."),
            ready(),
            a_plan(),
            editing_latest(instruction="Fix it."),
            ready(),
        ],
        asset_verifier=[a_verdict()],
    )
    world = World(["Lauch day", READ_BACK])

    Run(chat, world)

    for _, payload in world.asked:
        written = json.dumps(payload)
        assert "base64" not in written and "data:image" not in written
        assert len(written) < 8000


def test_the_cost_limit_refuses_a_generation_it_cannot_pay_for():
    chat = ScriptedChat(
        creative_director=[a_brief()],
        studio=[
            operation("generate_image", prompt="A desk."),
            operation("generate_image", prompt="Another desk."),
            ready(),
        ],
        asset_verifier=[a_verdict()],
    )
    world = World([READ_BACK], cost_usd=0.05)

    run = Run(chat, world, limits=Limits(max_cost_usd=0.10, image_estimate_usd=0.08))

    assert world.names() == [GENERATE_IMAGE, READ_TEXT]
    refused = chat.calls[3]["messages"][-1]
    assert refused["role"] == "tool" and "cost limit" in refused["content"]
    assert run.result.cost_usd == 0.05


KEY_TOO_LOW = "the OpenRouter key's balance or usage limit is too low for image output"
NO_SANDBOX = "the sandbox could not be started: the docker command was not found"


def test_an_operation_that_is_blocked_ends_the_run_with_its_reason_at_once():
    chat = ScriptedChat(
        creative_director=[a_brief()],
        studio=[operation("generate_image", prompt="A desk."), ready(), a_plan()],
    )
    world = World([], failing={GENERATE_IMAGE: [rules.BLOCKED + KEY_TOO_LOW]})

    with pytest.raises(StudioError) as raised:
        Run(chat, world)

    assert str(raised.value) == KEY_TOO_LOW
    assert world.names() == [GENERATE_IMAGE]
    assert chat.left() == {"studio": 2}


def test_words_that_cannot_be_read_for_want_of_a_sandbox_end_the_run_with_its_reason():
    chat = ScriptedChat(
        creative_director=[a_brief()],
        studio=[operation("generate_image", prompt="A desk."), ready(), a_plan()],
        asset_verifier=[a_verdict()],
    )
    world = World([], failing={READ_TEXT: [rules.BLOCKED + NO_SANDBOX]})

    with pytest.raises(StudioError) as raised:
        Run(chat, world)

    assert str(raised.value) == NO_SANDBOX
    assert world.names() == [GENERATE_IMAGE, READ_TEXT]
    assert chat.left() == {"studio": 1, "asset_verifier": 1}


def test_an_operation_that_failed_otherwise_is_told_to_the_studio_model_which_goes_on():
    refused = "the image model refused to make this image"
    chat = ScriptedChat(
        creative_director=[a_brief()],
        studio=[
            operation("generate_image", prompt="A desk."),
            operation("generate_image", prompt="A calmer desk."),
            ready(),
        ],
        asset_verifier=[a_verdict()],
    )
    world = World([READ_BACK], failing={GENERATE_IMAGE: [refused]})

    run = Run(chat, world)

    assert world.names() == [GENERATE_IMAGE, GENERATE_IMAGE, READ_TEXT]
    assert refused in json.dumps(chat.calls[2]["messages"])
    assert not run.result.needs_review
