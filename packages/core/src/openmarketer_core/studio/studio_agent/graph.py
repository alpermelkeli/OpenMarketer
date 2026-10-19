"""The creative studio's graph: its nodes, what follows what, and how one attempt asks the models.

This module is wiring only. The rules live in ``rules.py`` and the image checks in
``image_checks.py``; model access goes through ``ChatModel`` (the model router); the five
actions reach the image provider and the sandbox only through the workflow that runs them, and
a stored image is read through the attempt's context. The state holds plain data only.

    brief -> produce <-> operate
               |  \\
               |   +--> check --> verify --> decide --> finalize -> END
               |          |                   ^  |
               +----------+-------------------+  +--> edit_plan -> produce
                (no candidate)  (checks failed)  +--> escalate -> END

``brief`` has the creative director turn the request into a brief and choose the first
technique; it is shown the product's repository images on one contact sheet
(``contact_sheet.py``) and may choose some for the asset, and it keeps their list in the state
for the rest of the run. ``produce`` has the studio model choose the next operation (it may read
the brand kit and look at the run's images and the product's with tools on the way), and
``operate`` asks for it as an action: the choice is made in one node and asked for in the next,
because a node asks for the same thing every time it runs and a model does not answer the same
twice. An action whose failure nothing else could get past (``rules.BLOCKED``: the image
provider's key cannot pay, there is no sandbox) ends the run with its reason, in ``operate`` or
in ``check``. ``produce`` ends the round when the model names a candidate, when the round's
operations or the run's cost are used up, or when the model chooses nothing usable. ``check``
asks for the candidate's text to be read (when the request has copy) and measures it in code,
the read text as evidence only; ``verify`` shows it to the verifier, a different model, when its
file passed, and fails the copy (``COPY``) when a line OCR did not confirm is not what the
verifier reads letter by letter (``rules.copy_verdict``); ``decide``
applies ``rules.decide``; ``edit_plan`` has the studio model plan the next round; ``finalize``
and ``escalate`` set the result, which whoever runs the graph stores when it ends.

Every node is safe to run twice: a step that died before its checkpoint is taken again from
the same state, and the model calls are made for this attempt only.
"""

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from openmarketer_core.agent_runtime.agent import END
from openmarketer_core.agent_runtime.langgraph_steps import request_action
from openmarketer_core.agent_runtime.model_input import DoesNotFit, read_strict_json
from openmarketer_core.llm import ChatReply, image_part
from openmarketer_core.studio.contact_sheet import SheetImage, contact_sheet
from openmarketer_core.studio.failures import StudioError
from openmarketer_core.studio.image_checks import COPY, check_image, copy_matches
from openmarketer_core.studio.platforms import Platform, PlatformSpec, platform_spec
from openmarketer_core.studio.studio_agent import progress, rules
from openmarketer_core.studio.studio_agent.definitions import (
    EditImage,
    GenerateImage,
    ReadText,
    RenderHtml,
    RunPython,
    StudioContext,
    StudioState,
    agent,
)
from openmarketer_core.studio.studio_agent.rules import (
    Brief,
    EditPlan,
    Operation,
    References,
    RunImages,
    Technique,
    VerifierReport,
)

# The most of a handler's error the studio model is shown, in the round's operations.
MAX_ERROR_CHARS = 500

Problems = Callable[[Any], list[str]]


@dataclass
class _Spent:
    """What the model calls of one node cost, and the model that answered last."""

    cost_usd: float = 0.0
    model: str = ""

    def add(self, reply: ChatReply) -> None:
        self.cost_usd += reply.cost_usd
        self.model = reply.model or self.model

    def update(self, state: StudioState, cost_usd: float = 0.0) -> dict[str, Any]:
        return {
            "cost_usd": state["cost_usd"] + self.cost_usd + cost_usd,
            "model": self.model or state["model"],
        }


def _no_problems(submitted: Any) -> list[str]:
    return []


def _read[Model: BaseModel](
    arguments: Any, model_type: type[Model], problems: Problems
) -> Model | str:
    """The submission in ``arguments``, or the text that tells the model what was wrong."""
    if not isinstance(arguments, dict):
        return "error: the answer must be one JSON object"
    try:
        submitted = read_strict_json(model_type, rules.unwrapped_arguments(model_type, arguments))
    except DoesNotFit as e:
        return f"Rejected. Fix this and submit again: {e}"
    found = problems(submitted)
    if found:
        return "Rejected. Fix this and submit again: " + "; ".join(found)
    return submitted


def _arguments_of(call: dict[str, Any]) -> Any:
    try:
        return json.loads(call.get("function", {}).get("arguments") or "{}")
    except ValueError:
        return None


def _ask_in_text[Model: BaseModel](
    context: StudioContext,
    role: str,
    messages: list[dict[str, Any]],
    *,
    model_type: type[Model],
    spent: _Spent,
    problems: Problems = _no_problems,
) -> Model | None:
    """Ask a role that is sent no tools for one JSON object, at most twice; ``None`` if unread."""
    for _ in range(rules.SUBMISSION_ATTEMPTS):
        reply = context.model.chat(role, messages)
        spent.add(reply)
        try:
            parsed: Any = json.loads(reply.text)
        except ValueError:
            parsed = None
        submitted = _read(parsed, model_type, problems)
        if not isinstance(submitted, str):
            return submitted
        if reply.finish_reason == "length":
            submitted = f"The answer was cut off by the model's length limit. {submitted}"
        messages = [*messages, reply.message, {"role": "user", "content": submitted}]
    return None


def _ask_for[Model: BaseModel](
    context: StudioContext,
    role: str,
    messages: list[dict[str, Any]],
    *,
    tool: dict[str, Any],
    tool_name: str,
    model_type: type[Model],
    spent: _Spent,
    problems: Problems = _no_problems,
) -> Model | None:
    """Ask for one forced submission, at most twice; ``None`` when none could be read."""
    for _ in range(rules.SUBMISSION_ATTEMPTS):
        reply = context.model.chat(
            role, messages, tools=[tool], tool_choice=rules.forced(tool_name)
        )
        spent.add(reply)
        calls = [c for c in reply.tool_calls if c.get("function", {}).get("name") == tool_name]
        if calls:
            submitted = _read(_arguments_of(calls[0]), model_type, problems)
        else:
            submitted = f"Call {tool_name} with the fields it takes."
        if not isinstance(submitted, str):
            return submitted
        messages = [*messages, reply.message, *_answers(reply, submitted)]
    return None


def _answers(reply: ChatReply, text: str) -> list[dict[str, Any]]:
    """What answers every tool call of ``reply`` with ``text``, or a user message when none."""
    if not reply.tool_calls:
        return [{"role": "user", "content": text}]
    return [
        {"role": "tool", "tool_call_id": call.get("id", ""), "content": text}
        for call in reply.tool_calls
    ]


def _spec(state: StudioState) -> PlatformSpec:
    return platform_spec(Platform(state["platform"]))


def _shown_image(context: StudioContext, version_id: str) -> dict[str, Any]:
    image = context.image_of(version_id)
    return image_part(image.data, image.mime)


def _run_ids(state: StudioState) -> dict[str, uuid.UUID]:
    return {
        "workspace_id": uuid.UUID(state["workspace_id"]),
        "project_id": uuid.UUID(state["project_id"]),
        "run_id": uuid.UUID(state["run_id"]),
    }


def _repository_images(state: StudioState) -> list[dict[str, Any]]:
    # A thread stored before the run kept its repository images has none.
    return state.get("repository_images", [])


def _shown_repository_image(context: StudioContext, image_id: str) -> dict[str, Any] | None:
    image = context.repository_image_of(image_id)
    return None if image is None else image_part(image.data, image.mime)


def _sheet_of(context: StudioContext, listed: list[dict[str, Any]]) -> dict[str, Any]:
    """The contact sheet of ``listed``, as a part of a message; an image no longer there is
    drawn as unreadable."""
    images = []
    for shown in listed:
        found = context.repository_image_of(shown["id"])
        images.append(
            SheetImage(
                number=shown["number"],
                kind=shown["kind"],
                path=shown["path"],
                data=None if found is None else found.data,
            )
        )
    return image_part(contact_sheet(images), "image/png")


def _this_round(state: StudioState) -> list[str]:
    return [v["version_id"] for v in state["versions"] if v["round"] == state["round"]]


# ----------------------------------------------------------------------------------- brief
@agent.node(start=True)
def brief(state: StudioState, context: StudioContext) -> dict[str, Any]:
    """Have the creative director turn the request into a brief and choose the technique.

    The product's repository images are listed for this attempt; the list the director was
    shown is kept, so that the brief's choices and every later step name the same images.
    """
    listed = rules.repository_images_data(context.repository_images)
    messages = rules.brief_messages(
        state,
        _spec(state),
        context.brand_kit.as_data(),
        repository_images=listed,
        contact_sheet=_sheet_of(context, listed) if listed else None,
    )
    spent = _Spent()
    submitted = _ask_in_text(
        context,
        rules.CREATIVE_DIRECTOR_ROLE,
        messages,
        model_type=Brief,
        spent=spent,
        problems=lambda brief: rules.brief_problems(brief, listed),
    )
    if submitted is None:
        raise StudioError(
            "the creative director did not answer a readable brief after two attempts"
        )
    made = rules.numbered_as_ids(submitted, listed).model_dump(mode="json")
    real_images = bool(state["reference_asset_ids"]) or rules.chosen_screenshots(made, listed)
    return {
        "brief": made,
        "repository_images": listed,
        "about_product": rules.about_product(context.brand_kit, state["exact_copy"]),
        "technique": rules.first_technique(
            submitted.technique, has_reference_assets=real_images
        ).value,
        "round": 1,
        **spent.update(state),
    }


# --------------------------------------------------------------------------------- produce
@agent.node
def produce(state: StudioState, context: StudioContext) -> dict[str, Any]:
    """Have the studio model choose the round's next operation, or the round's candidate.

    The images a program or a page opens are added to its inputs (``rules.with_opened_inputs``),
    and a candidate a program made in the operation just before is refused once and shown
    again (``rules.needs_a_look``), for the model to check its words before they are read.
    """
    limits = context.limits
    images = _run_images(state)
    if len(state["operations"]) >= limits.max_operations_per_round:
        return _round_ends(
            state, context, images, f"round {state['round']} used all its operations"
        )
    if state["cost_usd"] >= limits.max_cost_usd:
        return _round_ends(
            state, context, images, f"round {state['round']} ended at the cost limit"
        )
    spent = _Spent()
    messages = _produce_messages(state, context)
    # A candidate a program just made is refused once, for the model to look at it again.
    looked_again = False
    for _ in range(rules.MAX_PRODUCE_CALLS):
        reply = context.model.chat(rules.STUDIO_ROLE, messages, tools=rules.produce_tools())
        spent.add(reply)
        messages = [*messages, reply.message]
        if not reply.tool_calls:
            messages.append(
                {"role": "user", "content": f"Call {rules.SUBMIT_OPERATION}, or one of the tools."}
            )
            continue
        shown: list[str] = []
        for call in reply.tool_calls:
            answer, chosen = _answer_call(call, state, context, images, shown)
            if chosen is not None and not looked_again and rules.needs_a_look(chosen, state):
                looked_again = True
                answer = rules.LOOK_FIRST
                shown.append(chosen.version_id)
                chosen = None
            if chosen is not None:
                return {**_chosen(state, chosen), **spent.update(state)}
            messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": answer})
        for image_id in shown:
            messages.append({"role": "user", "content": _viewed(context, images, image_id)})
    ended = _round_ends(
        state,
        context,
        images,
        f"round {state['round']}: the studio model chose no usable operation",
    )
    return {**ended, **spent.update(state)}


def _run_images(state: StudioState) -> RunImages:
    """The facts of the run's images an operation is checked against."""
    versions = state["versions"]
    by_image_model = frozenset(
        v["version_id"]
        for v in versions
        if v["operation"] in (rules.GENERATE_IMAGE, rules.EDIT_IMAGE)
    )
    # In the order they were made, so that a composite of a composite counts too. A version
    # stored before its inputs were kept names none.
    on_image_model: set[str] = set()
    for v in versions:
        inputs = set(v.get("input_version_ids", []))
        if v["operation"] == rules.RUN_PYTHON and inputs & (by_image_model | on_image_model):
            on_image_model.add(v["version_id"])
    return RunImages(
        run_versions=frozenset(v["version_id"] for v in versions),
        this_round=frozenset(_this_round(state)),
        reference_assets=frozenset(state["reference_asset_ids"]),
        made_by_code=frozenset(
            v["version_id"]
            for v in versions
            if v["operation"] in (rules.RUN_PYTHON, rules.RENDER_HTML)
        ),
        has_copy=bool(state["exact_copy"]),
        repository_images=frozenset(image["id"] for image in _repository_images(state)),
        renders_this_round=sum(
            1 for op in state["operations"] if op.get("ok") and op["op"] == rules.RENDER_HTML
        ),
        made_by_image_model=by_image_model,
        composited_on_image_model=frozenset(on_image_model),
    )


def _produce_messages(state: StudioState, context: StudioContext) -> list[dict[str, Any]]:
    data = rules.produce_data(state, _spec(state), context.limits, context.brand_kit.as_data())
    content: list[dict[str, Any]] = [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]
    latest = _this_round(state)
    shown = latest[-1] if latest else state["edit_plan"].get("source_version_id", "")
    if shown:
        content += [
            {"type": "text", "text": f"The latest image, {shown}:"},
            _shown_image(context, shown),
        ]
    content += _chosen_repository_images(state, context)
    return [
        {"role": "system", "content": rules.STUDIO_SYSTEM_PROMPT},
        {"role": "user", "content": content},
    ]


def _chosen_repository_images(state: StudioState, context: StudioContext) -> list[dict[str, Any]]:
    """The repository images the brief chose, shown with what each is for."""
    listed = {image["id"] for image in _repository_images(state)}
    content: list[dict[str, Any]] = []
    for reference in state["brief"].get("reference_images", []):
        if reference["id"] not in listed:
            continue
        part = _shown_repository_image(context, reference["id"])
        if part is None:
            continue
        caption = f"Repository image {reference['id']}, chosen for: {reference['use']}"
        content += [{"type": "text", "text": caption}, part]
    return content


def _viewed(context: StudioContext, images: RunImages, image_id: str) -> list[dict[str, Any]]:
    """What a model that asked to see ``image_id`` is shown."""
    if image_id in images.run_versions:
        return [{"type": "text", "text": f"Image {image_id}:"}, _shown_image(context, image_id)]
    part = _shown_repository_image(context, image_id)
    if part is None:
        return [{"type": "text", "text": f"Repository image {image_id} is no longer there."}]
    return [{"type": "text", "text": f"Repository image {image_id}:"}, part]


def _answer_call(
    call: dict[str, Any],
    state: StudioState,
    context: StudioContext,
    images: RunImages,
    shown: list[str],
) -> tuple[str, Operation | None]:
    """What a tool call is answered with, and the operation when it is a usable submission."""
    name = call.get("function", {}).get("name")
    arguments = _arguments_of(call)
    if name == rules.BRAND_KIT_TOOL:
        return json.dumps(context.brand_kit.as_data(), ensure_ascii=False), None
    if name == rules.VIEW_IMAGE_TOOL:
        image_id = arguments.get("version_id") if isinstance(arguments, dict) else None
        if not isinstance(image_id, str) or (
            image_id not in images.run_versions | images.repository_images
        ):
            return "error: that is not an image of this run nor a repository image", None
        shown.append(image_id)
        return "The image follows.", None
    if name != rules.SUBMIT_OPERATION:
        return f"error: there is no tool of that name; call {rules.SUBMIT_OPERATION}", None

    def problems(operation: Operation) -> list[str]:
        completed = rules.with_opened_inputs(operation, images)
        if completed.problems:
            return completed.problems
        return rules.operation_problems(
            completed.operation,
            technique=operation.technique or Technique(state["technique"]),
            images=images,
            cost_usd=state["cost_usd"],
            limits=context.limits,
        )

    submitted = _read(arguments, Operation, problems)
    if isinstance(submitted, str):
        return submitted, None
    # The images its code opens are its inputs, listed or not (``rules.with_opened_inputs``).
    return "", rules.with_opened_inputs(submitted, images).operation


def _chosen(state: StudioState, operation: Operation) -> dict[str, Any]:
    """The changes of a usable operation: a candidate named, or one waiting for ``operate``."""
    technique = (operation.technique or Technique(state["technique"])).value
    notes = state["notes"]
    if technique != state["technique"]:
        notes = [*notes, f"round {state['round']}: the studio switched to {technique}"]
    if operation.op == rules.CANDIDATE_READY:
        return {
            "candidate": operation.version_id,
            "pending": {},
            "technique": technique,
            "notes": notes,
        }
    fields = {
        rules.GENERATE_IMAGE: (
            "prompt",
            "reference_version_ids",
            "reference_asset_ids",
            "reference_repository_image_ids",
        ),
        rules.EDIT_IMAGE: (
            "source_version_id",
            "instruction",
            "reference_version_ids",
            "reference_asset_ids",
            "reference_repository_image_ids",
        ),
        rules.RENDER_HTML: ("html", "input_version_ids", "input_repository_image_ids"),
        rules.RUN_PYTHON: (
            "code",
            "requirements",
            "input_version_ids",
            "input_repository_image_ids",
        ),
    }[operation.op]
    pending = {
        "op": operation.op,
        "version_id": str(uuid.uuid4()),
        "technique": technique,
        "note": operation.note,
        **{name: getattr(operation, name) for name in fields},
    }
    if operation.op in rules.SPENDING_OPERATIONS:
        # Kept with the operation, so that its prompt is the same on every run of ``operate``.
        pending["crop_safe"] = True
    return {"pending": pending, "technique": technique, "notes": notes}


def _round_ends(
    state: StudioState, context: StudioContext, images: RunImages, note: str
) -> dict[str, Any]:
    """The round ends without a choice: its latest image is the candidate when the model could
    have named it with candidate_ready, under the same rules (``rules.operation_problems``).

    Otherwise the round has no candidate (``rules.NO_CANDIDATE``), and the check and the note
    say why, for the edit plan to mend it: a background without its words, or a rendered
    layout, is never a candidate by default.
    """
    latest = _this_round(state)
    notes = [*state["notes"], note]
    problems = ["the round made no image"]
    if latest:
        problems = rules.operation_problems(
            Operation(op=rules.CANDIDATE_READY, version_id=latest[-1]),
            technique=Technique(state["technique"]),
            images=images,
            cost_usd=state["cost_usd"],
            limits=context.limits,
        )
        if not problems:
            return {"candidate": latest[-1], "pending": {}, "notes": notes}
        notes.append(
            f"round {state['round']}: its latest image is not a candidate: " + "; ".join(problems)
        )
    check = {
        "version_id": "",
        "passed": False,
        "failures": [rules.NO_CANDIDATE],
        "why": "; ".join(problems),
    }
    return {"candidate": "", "pending": {}, "check": check, "verification": {}, "notes": notes}


# --------------------------------------------------------------------------------- operate
@agent.node
def operate(state: StudioState, context: StudioContext) -> dict[str, Any]:
    """Ask for the operation ``produce`` chose; keep the version it made, or why it did not."""
    pending = state["pending"]
    outcome = request_action(_payload_of(state, pending))
    if not outcome.succeeded:
        _end_if_blocked(outcome.error)
        error = rules.with_failure_hint(outcome.error, MAX_ERROR_CHARS)
        failed = {"op": pending["op"], "ok": False, "error": error}
        return {"operations": [*state["operations"], failed], "pending": {}}
    result = outcome.result
    version = {
        "version_id": pending["version_id"],
        "operation": pending["op"],
        "technique": pending["technique"],
        "round": state["round"],
        "width": int(result.get("width", 0)),
        "height": int(result.get("height", 0)),
        "mime": str(result.get("mime", "")),
        "size_bytes": int(result.get("size_bytes", 0)),
        "cost_usd": float(result.get("cost_usd", 0.0)),
        # What code made it on top of: a composite of the image model's image is its candidate.
        "input_version_ids": list(pending.get("input_version_ids", [])),
    }
    done = {"op": pending["op"], "version_id": pending["version_id"], "ok": True}
    return {
        "versions": [*state["versions"], version],
        "asset_id": str(result.get("asset_id") or state["asset_id"]),
        "operations": [*state["operations"], done],
        "pending": {},
        "cost_usd": state["cost_usd"] + version["cost_usd"],
    }


def _end_if_blocked(error: str) -> None:
    """Raise ``StudioError`` with the reason when an action's failure ends the run
    (``rules.BLOCKED``): no other operation or round would get past it."""
    reason = rules.blocked_reason(error)
    if reason is not None:
        raise StudioError(reason)


def _payload_of(state: StudioState, pending: dict[str, Any]) -> BaseModel:
    """The action ``pending`` names, built from the state alone, the same on every run."""
    technique = Technique(pending["technique"])
    common = {
        **_run_ids(state),
        "version_id": uuid.UUID(pending["version_id"]),
        "asset_id": uuid.UUID(state["asset_id"]) if state["asset_id"] else None,
        "round": state["round"],
        "technique": technique,
    }

    def ids(name: str) -> list[uuid.UUID]:
        # A pending operation stored before repository images were kept names none.
        return [uuid.UUID(value) for value in pending.get(name, [])]

    def lines(first_number: int) -> str:
        references = References(
            version_ids=pending["reference_version_ids"],
            asset_ids=pending["reference_asset_ids"],
            repository_image_ids=pending.get("reference_repository_image_ids", []),
        )
        return rules.reference_lines(
            references,
            technique=technique,
            brief=state["brief"],
            listed=_repository_images(state),
            first_number=first_number,
        )

    # What code adds to every prompt. A thread stored before the brief kept ``about_product``
    # has none, and its prompts go without it; an operation chosen before prompts said what a
    # square crop keeps (``crop_safe``) is asked as it was then.
    added = {
        "copy_placement": str(state["brief"].get("copy_placement", "")),
        "about": state.get("about_product", ""),
    }
    crop_to = _spec(state) if pending.get("crop_safe") else None
    op = pending["op"]
    if op == rules.GENERATE_IMAGE:
        return GenerateImage(
            **common,
            prompt=rules.generation_prompt(
                pending["prompt"],
                state["exact_copy"],
                technique,
                **added,
                references=lines(1),
                crop_to=crop_to,
            ),
            reference_version_ids=ids("reference_version_ids"),
            reference_asset_ids=ids("reference_asset_ids"),
            reference_repository_image_ids=ids("reference_repository_image_ids"),
        )
    if op == rules.EDIT_IMAGE:
        # The image it changes is sent first, so the references are numbered from 2.
        return EditImage(
            **common,
            source_version_id=uuid.UUID(pending["source_version_id"]),
            instruction=rules.edit_instruction(
                pending["instruction"],
                state["exact_copy"],
                technique,
                **added,
                references=lines(2),
                crop_to=crop_to,
            ),
            reference_version_ids=ids("reference_version_ids"),
            reference_asset_ids=ids("reference_asset_ids"),
            reference_repository_image_ids=ids("reference_repository_image_ids"),
        )
    if op == rules.RENDER_HTML:
        return RenderHtml(
            **common,
            html=pending["html"],
            input_version_ids=ids("input_version_ids"),
            input_repository_image_ids=ids("input_repository_image_ids"),
        )
    return RunPython(
        **common,
        code=pending["code"],
        requirements=list(pending["requirements"]),
        input_version_ids=ids("input_version_ids"),
        input_repository_image_ids=ids("input_repository_image_ids"),
    )


# ----------------------------------------------------------------------------------- check
@agent.node
def check(state: StudioState, context: StudioContext) -> dict[str, Any]:
    """Measure the candidate in code: its file against the platform, its words against the copy.

    The words are read by OCR, an action, only when the request has copy. OCR is evidence: a
    line it did not read exactly is recorded (``rules.COPY_UNCONFIRMED``) and left to the
    verifier, and does not fail the check.
    """
    candidate = state["candidate"]
    failures: list[str] = []
    notices: list[str] = []
    copy: dict[str, Any] | None = None
    if state["exact_copy"]:
        outcome = request_action(ReadText(**_run_ids(state), version_id=uuid.UUID(candidate)))
        if outcome.succeeded:
            matched = copy_matches(str(outcome.result.get("text", "")), state["exact_copy"])
            copy = matched.as_data()
            notices += [] if matched.all_confirmed else [rules.COPY_UNCONFIRMED]
        else:
            _end_if_blocked(outcome.error)
            copy = {"passed": False, "unread": True}
            failures.append(rules.COPY_UNREAD)
    image = context.image_of(candidate)
    measured = check_image(image.data, _spec(state))
    failures = [*measured.failures, *failures]
    report = {
        "version_id": candidate,
        "passed": not failures,
        "failures": failures,
        "notices": notices,
        "width": measured.width,
        "height": measured.height,
        "size_bytes": measured.size_bytes,
        "mime": measured.mime,
        "copy": copy,
        # The OCR gives no positions: the verifier judges the margins (``image_checks.py``).
        "safe_zone": "not measured",
    }
    version = next(v for v in state["versions"] if v["version_id"] == candidate)
    entry = {
        "version_id": candidate,
        "round": state["round"],
        "technique": version["technique"],
        "check": report,
        "verifier": None,
        "passed": False,
        "failures": failures,
    }
    return {"check": report, "verification": {}, "candidates": [*state["candidates"], entry]}


# ---------------------------------------------------------------------------------- verify
@agent.node
def verify(state: StudioState, context: StudioContext) -> dict[str, Any]:
    """Show the candidate to the verifier, a different model, with the brief and the checks."""
    spent = _Spent()
    data = rules.verifier_data(state, _spec(state))
    messages = [
        {"role": "system", "content": rules.VERIFIER_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": json.dumps(data, ensure_ascii=False)},
                _shown_image(context, state["candidate"]),
                # The product's real images the brief chose, to tell an imitation from them.
                *_chosen_repository_images(state, context),
            ],
        },
    ]
    exact_copy, copy_check = state["exact_copy"], state["check"].get("copy")
    to_transcribe = rules.unconfirmed_lines(exact_copy, copy_check)
    submitted = _ask_in_text(
        context,
        rules.VERIFIER_ROLE,
        messages,
        model_type=VerifierReport,
        spent=spent,
        problems=lambda report: rules.verifier_problems(report, to_transcribe=to_transcribe),
    )
    entry = dict(state["candidates"][-1])
    if submitted is None:
        report: dict[str, Any] = {}
        failures = [rules.VERIFIER_UNREAD]
        notes = [*state["notes"], f"round {state['round']}: the verifier's report was unreadable"]
    else:
        report = submitted.model_dump(mode="json")
        verdict = rules.copy_verdict(exact_copy, copy_check, report)
        failures = rules.verifier_failures(report)
        if any(not line["matches"] for line in verdict):
            failures.append(COPY)
        report["copy_verdict"] = verdict
        notes = state["notes"]
    entry.update(
        verifier=report or None, passed=not failures, failures=[*entry["failures"], *failures]
    )
    return {
        "verification": report,
        "candidates": [*state["candidates"][:-1], entry],
        "notes": notes,
        **spent.update(state),
    }


# ---------------------------------------------------------------------------------- decide
@agent.node
def decide(state: StudioState, context: StudioContext) -> dict[str, Any]:
    """Apply the decide rule to the round: finalize, edit (perhaps another technique), escalate."""
    latest = state["candidates"][-1] if state["candidates"] else None
    if latest is not None and latest["round"] == state["round"] and state["candidate"]:
        passed, failures = bool(latest["passed"]), list(latest["failures"])
    else:
        passed, failures = False, list(state["check"].get("failures", [rules.NO_CANDIDATE]))
    technique = Technique(state["technique"])
    decision = rules.decide(
        passed=passed,
        failures=failures,
        technique=technique,
        earlier=state["failures"],
        round_no=state["round"],
        cost_usd=state["cost_usd"],
        limits=context.limits,
    )
    update: dict[str, Any] = {
        "decision": decision.next,
        "reason": decision.reason,
        "technique": decision.technique.value,
    }
    if not passed:
        failed = {"round": state["round"], "technique": technique.value, "failures": failures}
        update["failures"] = [*state["failures"], failed]
    if decision.technique != technique:
        update["notes"] = [*state["notes"], f"round {state['round']}: {decision.reason}"]
    return update


# ------------------------------------------------------------------------------- edit_plan
@agent.node
def edit_plan(state: StudioState, context: StudioContext) -> dict[str, Any]:
    """Have the studio model plan the next round from the failures and the edit strategies."""
    spec = _spec(state)
    failures = state["failures"][-1]["failures"]
    versions = {v["version_id"] for v in state["versions"]}
    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": json.dumps(rules.edit_plan_data(state, spec, failures), ensure_ascii=False),
        }
    ]
    if state["candidate"]:
        content.append(_shown_image(context, state["candidate"]))
    messages = [
        {"role": "system", "content": rules.EDIT_PLAN_SYSTEM_PROMPT},
        {"role": "user", "content": content},
    ]

    def problems(plan: EditPlan) -> list[str]:
        unknown = plan.source_version_id and plan.source_version_id not in versions
        return ["source_version_id is not an image of this run"] if unknown else []

    spent = _Spent()
    submitted = _ask_for(
        context,
        rules.STUDIO_ROLE,
        messages,
        tool=rules.edit_plan_tool(),
        tool_name=rules.SUBMIT_EDIT_PLAN,
        model_type=EditPlan,
        spent=spent,
        problems=problems,
    )
    notes = state["notes"]
    if submitted is None:
        # The table of edit strategies is a plan of its own; the run goes on with it.
        steps = rules.edit_strategies(
            failures,
            Technique(state["technique"]),
            spec,
            real_assets=rules.chosen_real_assets(state),
        ) or ["Make the image again, closer to the brief."]
        plan = {
            "summary": "Mend the failures of the last round.",
            "steps": steps[: rules.MAX_PLAN_STEPS],
            "source_version_id": state["candidate"],
        }
        notes = [*notes, f"round {state['round']}: the edit plan was made from the strategies"]
    else:
        plan = submitted.model_dump(mode="json")
    return {
        "edit_plan": plan,
        "round": state["round"] + 1,
        "operations": [],
        "pending": {},
        "candidate": "",
        "check": {},
        "verification": {},
        "notes": notes,
        **spent.update(state),
    }


# ------------------------------------------------------------------------ finalize/escalate
@agent.node
def finalize(state: StudioState) -> dict[str, Any]:
    """The candidate passed: the run ends with it."""
    result = {
        "final_version_id": state["candidate"],
        "needs_review": False,
        "reason": state["reason"],
    }
    return {"result": result}


@agent.node
def escalate(state: StudioState) -> dict[str, Any]:
    """A limit was hit: the run ends with its best candidate, for a person to decide on.

    Raises ``StudioError`` when the run made no image at all.
    """
    best = rules.best_candidate(state["candidates"])
    if best is not None:
        final = best["version_id"]
    elif state["versions"]:
        final = state["versions"][-1]["version_id"]
    else:
        raise StudioError(f"no image could be made: {state['reason']}")
    return {"result": {"final_version_id": final, "needs_review": True, "reason": state["reason"]}}


agent.progress(progress.progress_of)
agent.edge(brief, produce)
agent.edge(operate, produce)
agent.edge(verify, decide)
agent.edge(edit_plan, produce)
agent.edge(finalize, END)
agent.edge(escalate, END)


@agent.route(after=produce, to=[operate, check, decide])
def after_produce(state: StudioState) -> str:
    if state["pending"]:
        return operate.__name__
    return check.__name__ if state["candidate"] else decide.__name__


@agent.route(after=check, to=[verify, decide])
def after_check(state: StudioState) -> str:
    return verify.__name__ if state["check"]["passed"] else decide.__name__


@agent.route(after=decide, to=[finalize, edit_plan, escalate])
def after_decide(state: StudioState) -> str:
    return {
        "finalize": finalize.__name__,
        "edit": edit_plan.__name__,
        "escalate": escalate.__name__,
    }[state["decision"]]


def recursion_limit(limits: rules.Limits) -> int:
    """How many graph steps a run under ``limits`` may take before it is stopped as a defect."""
    # a produce and an operate per operation, the produce that ends the round, check, verify,
    # decide and edit_plan
    per_round = 2 * limits.max_operations_per_round + 5
    return per_round * limits.max_rounds + 10
