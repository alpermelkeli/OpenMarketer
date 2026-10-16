"""Tests for the framework-free half of the agent runtime: actions, steps and thread names.

``test_agent.py`` covers ``Agent``, ``test_tools.py`` its tools and ``test_model_input.py`` how
what a model wrote is read; ``test_graph_thread.py`` and ``test_langgraph_agent.py`` cover the
LangGraph half.
"""

import ast
import dataclasses
import datetime
import enum
import hashlib
import importlib
import inspect
import math
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Annotated, Any

import pytest
from pydantic import BaseModel, ConfigDict, Field, ImportString, create_model
from pydantic import dataclasses as pydantic_dataclasses

from openmarketer_core import agent_runtime
from openmarketer_core.agent_runtime import actions, agent, gate, model_input, steps, threads, tools
from openmarketer_core.agent_runtime.actions import (
    MAX_ATTEMPT_TIMEOUT_SECONDS,
    MAX_ATTEMPTS,
    MAX_RETRY_AFTER_SECONDS,
    ActionCatalog,
    ActionDefinition,
    ActionOutcome,
    ActionPolicy,
    ActionRequest,
    ChildRun,
    InvalidActionDefinition,
    InvalidActionPayload,
    RunKind,
    RunTarget,
    UnknownAction,
    request_digest,
    require_positive,
)
from openmarketer_core.agent_runtime.model_input import (
    MAX_SHOWN_CHARACTERS,
    MAX_SHOWN_FIELDS,
    UNKNOWN_FIELD,
)
from openmarketer_core.agent_runtime.steps import StepOutcome
from openmarketer_core.agent_runtime.threads import thread_id
from openmarketer_core.db.analysis_runs import ANALYSIS_RUN_THREADS, run_thread_id

SECRET = "sk-live-4242-do-not-repeat"


class Recipient(BaseModel):
    model_config = ConfigDict(extra="forbid")

    address: str


class SendEmail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to: list[Recipient]
    subject: str
    attempts: int = 1


class Publish(BaseModel):
    model_config = ConfigDict(extra="forbid")

    post_id: str


CATALOG = ActionCatalog(
    [ActionDefinition("send_email", SendEmail), ActionDefinition("publish", Publish)]
)


def email(**changes) -> ActionRequest:
    payload = {"to": [{"address": "a@example.com"}], "subject": "Hello"} | changes
    return ActionRequest(action_id="analysis-run:r:i", name="send_email", payload=payload)


# ------------------------------------------------------------------ the catalog
def test_valid_request_is_returned_as_the_model_of_its_action():
    payload = CATALOG.validate(email(attempts=3))
    assert payload == SendEmail(
        to=[Recipient(address="a@example.com")], subject="Hello", attempts=3
    )


def test_action_that_is_not_in_the_catalog_is_refused():
    request = ActionRequest(action_id="t:i", name="delete_everything", payload={})
    with pytest.raises(UnknownAction, match="delete_everything") as refused:
        CATALOG.validate(request)
    assert refused.value.action_name == "delete_everything"


def test_name_of_an_unknown_action_is_shortened_in_the_message():
    request = ActionRequest(action_id="t:i", name="x" * 1000 + "\nforged log line", payload={})
    with pytest.raises(UnknownAction) as refused:
        CATALOG.validate(request)
    assert len(str(refused.value)) < 2 * MAX_SHOWN_CHARACTERS
    assert "\n" not in str(refused.value)


def test_field_that_the_model_does_not_have_is_refused():
    with pytest.raises(InvalidActionPayload, match="send_email") as refused:
        CATALOG.validate(email(cc="boss@example.com"))
    assert refused.value.fields == (UNKNOWN_FIELD,)


def test_field_of_the_wrong_type_is_refused():
    with pytest.raises(InvalidActionPayload) as refused:
        CATALOG.validate(email(attempts="many"))
    assert refused.value.fields == ("attempts",)


def test_field_that_is_missing_is_refused():
    request = ActionRequest(action_id="t:i", name="publish", payload={})
    with pytest.raises(InvalidActionPayload) as refused:
        CATALOG.validate(request)
    assert refused.value.fields == ("post_id",)


def test_problem_inside_a_nested_value_names_the_path_to_it():
    with pytest.raises(InvalidActionPayload) as refused:
        CATALOG.validate(email(to=[{"address": "a@example.com"}, {"address": 7}]))
    assert refused.value.fields == ("to.1.address",)


@pytest.mark.parametrize(
    "changes",
    [
        {"attempts": SECRET},
        {"token": SECRET},
        {"to": [{"address": "a@example.com", "key": SECRET}]},
    ],
    ids=["wrong type", "extra field", "extra nested field"],
)
def test_refusal_names_the_field_and_never_repeats_what_was_sent(changes):
    with pytest.raises(InvalidActionPayload) as refused:
        CATALOG.validate(email(**changes))
    assert SECRET not in str(refused.value)
    assert SECRET not in repr(refused.value.fields)


def test_refusal_does_not_carry_the_librarys_error_that_holds_the_values():
    with pytest.raises(InvalidActionPayload) as refused:
        CATALOG.validate(email(attempts=SECRET))
    assert refused.value.__cause__ is None
    assert refused.value.__suppress_context__


def test_message_lists_only_the_first_fields_when_there_are_many():
    fields: dict[str, Any] = {f"field_{n}": (int, ...) for n in range(100)}
    many = create_model("Many", __config__=ConfigDict(extra="forbid"), **fields)
    catalog = ActionCatalog([ActionDefinition("many", many)])
    request = ActionRequest("t:i", "many", {f"field_{n}": SECRET for n in range(100)})
    with pytest.raises(InvalidActionPayload) as refused:
        catalog.validate(request)
    assert len(refused.value.fields) == 100
    assert f"and {100 - MAX_SHOWN_FIELDS} more" in str(refused.value)
    assert "field_99" not in str(refused.value)


def test_refusal_does_not_repeat_a_field_the_model_made_up():
    with pytest.raises(InvalidActionPayload) as refused:
        CATALOG.validate(email(**{SECRET: "x", "to": [{"address": "a@example.com", SECRET: "y"}]}))
    assert refused.value.fields == (UNKNOWN_FIELD, f"to.0.{UNKNOWN_FIELD}")
    assert SECRET not in str(refused.value)


def test_two_definitions_of_one_action_are_refused():
    with pytest.raises(InvalidActionDefinition, match="send_email"):
        ActionCatalog(
            [ActionDefinition("send_email", SendEmail), ActionDefinition("send_email", Publish)]
        )


@pytest.mark.parametrize("extra", [None, "ignore", "allow"])
def test_payload_model_that_allows_extra_fields_is_refused(extra):
    config = ConfigDict() if extra is None else ConfigDict(extra=extra)
    lenient = type("Lenient", (BaseModel,), {"model_config": config, "__annotations__": {}})
    with pytest.raises(InvalidActionDefinition, match="forbid extra fields"):
        ActionDefinition("lenient", lenient)


def test_payload_model_that_forbids_extra_fields_through_a_parent_is_accepted():
    class Child(Publish):
        pass

    assert ActionDefinition("child", Child).payload is Child


def test_payload_that_holds_a_dataclass_that_holds_a_model_that_allows_extra_fields_is_refused():
    @dataclasses.dataclass
    class Wrapper:
        inner: Lenient

    with pytest.raises(InvalidActionDefinition, match="Lenient in the payload of action 'wrap'"):
        ActionDefinition("wrap", holding(list[Wrapper]))


def test_payload_that_holds_an_alias_of_a_model_that_allows_extra_fields_is_refused():
    with pytest.raises(InvalidActionDefinition, match="Lenient.*forbid extra fields"):
        ActionDefinition("alias", holding(LenientAlias))


def test_payload_that_holds_a_pydantic_dataclass_that_allows_extra_fields_is_refused():
    @pydantic_dataclasses.dataclass
    class Loose:
        value: str

    with pytest.raises(InvalidActionDefinition, match="Loose.*forbid extra fields"):
        ActionDefinition("loose", holding(Loose))


def test_payload_with_a_field_that_could_name_a_file_is_refused_naming_the_field():
    class Upload(BaseModel):
        model_config = ConfigDict(extra="forbid")

        destination: Path

    with pytest.raises(
        InvalidActionDefinition,
        match="field 'destination' of Upload in the payload of action 'upload' reads text",
    ):
        ActionDefinition("upload", Upload)


def test_payload_with_a_field_that_could_name_code_is_refused_naming_the_field():
    class Hook(BaseModel):
        model_config = ConfigDict(extra="forbid")

        target: ImportString

    class Subscribe(BaseModel):
        model_config = ConfigDict(extra="forbid")

        hooks: list[Hook]

    with pytest.raises(InvalidActionDefinition, match="field 'target' of Hook"):
        ActionDefinition("subscribe", Subscribe)


def test_payload_that_holds_a_type_that_is_not_defined_is_refused_and_not_taken_for_a_fine_one():
    class Unfinished(BaseModel):
        model_config = ConfigDict(extra="forbid")

        inner: "NotDefinedAnywhere"  # type: ignore[name-defined]  # noqa: F821

    with pytest.raises(
        InvalidActionDefinition,
        match="payload of action 'unfinished'.*'NotDefinedAnywhere' is not defined",
    ):
        ActionDefinition("unfinished", Unfinished)


def test_payload_with_the_text_of_a_path_as_text_is_accepted():
    class Upload(BaseModel):
        model_config = ConfigDict(extra="forbid")

        destination: str

    assert ActionDefinition("upload", Upload).payload is Upload


# ------------------------------------------------- a payload is read as strict JSON
class Color(enum.Enum):
    RED = "red"


class Schedule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    post_id: uuid.UUID
    at: datetime.datetime
    on: datetime.date
    color: Color
    enabled: bool
    count: int
    ratio: float


SCHEDULE = ActionCatalog([ActionDefinition("schedule", Schedule)])
JSON_OF_A_SCHEDULE = {
    "post_id": "5908bdbd-68bf-4c0b-bc64-7c881d190276",
    "at": "2026-10-11T08:00:00Z",
    "on": "2026-10-11",
    "color": "red",
    "enabled": True,
    "count": 7,
    "ratio": 2,
}


def schedule(**changes) -> ActionRequest:
    return ActionRequest("t:i", "schedule", JSON_OF_A_SCHEDULE | changes)


def test_text_is_accepted_for_the_types_that_are_text_in_json():
    payload = SCHEDULE.validate(schedule())
    assert isinstance(payload, Schedule)
    assert payload.post_id == uuid.UUID("5908bdbd-68bf-4c0b-bc64-7c881d190276")
    assert payload.at == datetime.datetime(2026, 10, 11, 8, tzinfo=datetime.UTC)
    assert payload.on == datetime.date(2026, 10, 11)
    assert payload.color is Color.RED
    assert payload.ratio == 2.0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("enabled", "yes"),
        ("enabled", 1),
        ("count", "7"),
        ("count", 7.0),
        ("count", True),
        ("ratio", "0.5"),
        ("post_id", 7),
        ("color", "blue"),
    ],
)
def test_value_of_another_type_is_refused_and_not_converted(field, value):
    with pytest.raises(InvalidActionPayload) as refused:
        SCHEDULE.validate(schedule(**{field: value}))
    assert refused.value.fields == (field,)


@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), float("-inf")],
    ids=["nan", "inf", "-inf"],
)
def test_number_that_json_has_no_way_to_write_is_refused(value):
    with pytest.raises(InvalidActionPayload) as refused:
        SCHEDULE.validate(schedule(ratio=value))
    assert refused.value.fields == ("ratio",)


@pytest.mark.parametrize(
    "value",
    [b"bytes", {1, 2}, object(), {"nested": [float("nan")]}],
    ids=["bytes", "set", "object", "nan deep inside"],
)
def test_value_that_is_not_json_is_refused_as_a_payload_and_not_as_a_crash(value):
    with pytest.raises(InvalidActionPayload) as refused:
        SCHEDULE.validate(schedule(color=value))
    assert refused.value.fields == ("color",)


def test_refusal_of_a_payload_that_is_not_json_does_not_repeat_what_was_sent():
    with pytest.raises(InvalidActionPayload) as refused:
        SCHEDULE.validate(schedule(color={"key": SECRET, "number": float("nan")}))
    assert SECRET not in str(refused.value)
    assert refused.value.__cause__ is None


# ----------------------------------------- every model of a payload forbids extra fields
class Lenient(BaseModel):
    """Unknown keys are silently dropped, which is the library's default."""

    value: str


class Forbidding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str


type LenientAlias = Lenient


class Tree(BaseModel):
    """Lenient, and refers to itself."""

    children: list["Tree"] = []


class StrictTree(BaseModel):
    model_config = ConfigDict(extra="forbid")

    children: list["StrictTree"] = []


def holding(annotation) -> type[BaseModel]:
    return create_model("Holder", __config__=ConfigDict(extra="forbid"), field=(annotation, ...))


@pytest.mark.parametrize(
    "annotation",
    [
        Lenient,
        Lenient | None,
        list[Lenient],
        dict[str, Lenient],
        tuple[Lenient, ...],
        Forbidding | Lenient,
        Annotated[Lenient, Field(description="a nested model")],
        list[dict[str, list[Lenient | None]]],
    ],
    ids=[
        "plain",
        "optional",
        "list",
        "dict",
        "tuple",
        "union",
        "annotated",
        "deeply nested",
    ],
)
def test_nested_model_that_allows_extra_fields_is_refused_wherever_it_is_held(annotation):
    with pytest.raises(InvalidActionDefinition, match="Lenient.*forbid extra fields"):
        ActionDefinition("holder", holding(annotation))


def test_model_that_is_nested_two_levels_down_is_refused():
    middle = create_model("Middle", __config__=ConfigDict(extra="forbid"), inner=(Lenient, ...))
    with pytest.raises(InvalidActionDefinition, match="Lenient"):
        ActionDefinition("holder", holding(list[middle]))


def test_nested_models_that_forbid_extra_fields_are_accepted():
    definition = ActionDefinition("holder", holding(list[Forbidding] | None))
    assert definition.name == "holder"


def test_model_that_refers_to_itself_is_checked_once_and_does_not_loop():
    assert ActionDefinition("tree", StrictTree).payload is StrictTree
    with pytest.raises(InvalidActionDefinition, match="Tree"):
        ActionDefinition("tree", Tree)


def test_unknown_key_in_a_nested_model_of_a_payload_is_refused_when_it_is_asked():
    catalog = ActionCatalog([ActionDefinition("holder", holding(list[Forbidding]))])
    request = ActionRequest("t:i", "holder", {"field": [{"value": "a", "key": SECRET}]})
    with pytest.raises(InvalidActionPayload) as refused:
        catalog.validate(request)
    assert refused.value.fields == (f"field.0.{UNKNOWN_FIELD}",)
    assert SECRET not in str(refused.value)


# ------------------------------------------------------------- how an action is tried
def test_action_policy_without_a_heartbeat_has_none():
    assert ActionPolicy(30, 2, 1).heartbeat_timeout_seconds is None


def limits_with(**changes) -> dict[str, Any]:
    return {"attempt_timeout_seconds": 30, "max_attempts": 2, "retry_after_seconds": 1} | changes


@pytest.mark.parametrize(
    "limits",
    [
        {"attempt_timeout_seconds": 0},
        {"attempt_timeout_seconds": -1},
        {"max_attempts": 0},  # Temporal reads 0 attempts as no limit, which an action must not have
        {"retry_after_seconds": 0},
        {"heartbeat_timeout_seconds": 0},
        {"heartbeat_timeout_seconds": -1},
    ],
)
def test_action_policy_with_a_limit_that_is_not_positive_is_refused(limits):
    with pytest.raises(ValueError, match=next(iter(limits))):
        ActionPolicy(**limits_with(**limits))


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf], ids=["nan", "inf", "-inf"])
@pytest.mark.parametrize(
    "limit",
    ["attempt_timeout_seconds", "retry_after_seconds", "heartbeat_timeout_seconds"],
)
def test_action_policy_with_a_number_that_is_not_finite_is_refused(limit, value):
    # ``timedelta(seconds=nan)`` raises inside the workflow, which fails the task again and again.
    with pytest.raises(ValueError, match=f"{limit} must be a finite number"):
        ActionPolicy(**limits_with(**{limit: value}))


@pytest.mark.parametrize(
    ("limit", "value"),
    [
        ("max_attempts", True),
        ("max_attempts", 2.0),
        ("max_attempts", "2"),
        ("attempt_timeout_seconds", True),
        ("attempt_timeout_seconds", "30"),
        ("retry_after_seconds", None),
    ],
    ids=repr,
)
def test_action_policy_with_a_value_that_is_not_a_number_of_the_right_kind_is_refused(limit, value):
    with pytest.raises(ValueError, match=limit):
        ActionPolicy(**limits_with(**{limit: value}))


@pytest.mark.parametrize(
    ("limit", "ceiling"),
    [
        ("max_attempts", MAX_ATTEMPTS),
        ("attempt_timeout_seconds", MAX_ATTEMPT_TIMEOUT_SECONDS),
        ("retry_after_seconds", MAX_RETRY_AFTER_SECONDS),
    ],
)
def test_action_policy_goes_up_to_its_ceiling_and_no_further(limit, ceiling):
    assert ActionPolicy(**limits_with(**{limit: ceiling})) is not None
    with pytest.raises(ValueError, match=f"{limit} must be at most {ceiling}"):
        ActionPolicy(**limits_with(**{limit: ceiling + 1}))


def test_ceilings_are_ten_attempts_a_day_for_an_attempt_and_an_hour_between_attempts():
    assert (MAX_ATTEMPTS, MAX_ATTEMPT_TIMEOUT_SECONDS, MAX_RETRY_AFTER_SECONDS) == (
        10,
        86_400,
        3_600,
    )


def test_heartbeat_is_not_longer_than_the_attempt_it_watches():
    assert ActionPolicy(**limits_with(heartbeat_timeout_seconds=30)).heartbeat_timeout_seconds == 30
    with pytest.raises(ValueError, match="heartbeat_timeout_seconds must be at most 30"):
        ActionPolicy(**limits_with(heartbeat_timeout_seconds=31))


def test_action_policy_cannot_be_changed_once_it_is_made():
    policy = ActionPolicy(30, 2, 1)
    with pytest.raises(AttributeError):
        policy.max_attempts = 0  # pyright: ignore[reportAttributeAccessIssue]


@pytest.mark.parametrize("value", [math.nan, math.inf, True, 0, -1, "1"], ids=repr)
def test_limit_that_is_not_a_finite_number_above_zero_is_refused_naming_it(value):
    with pytest.raises(ValueError, match="own_limit must be a finite number more than 0"):
        require_positive(own_limit=value)


def test_every_limit_that_is_not_positive_is_checked_not_only_the_first():
    require_positive(first=1, second=0.5)
    with pytest.raises(ValueError, match="second"):
        require_positive(first=1, second=0)


# ------------------------------------------------------- what a request is, as a digest
def test_digest_of_a_request_is_the_sha256_of_its_name_and_payload_as_compact_sorted_json():
    digest = request_digest("send_email", {"to": "ada", "attempts": 2})
    canonical = b'{"name":"send_email","payload":{"attempts":2,"to":"ada"}}'
    assert digest == hashlib.sha256(canonical).hexdigest()


def test_digest_of_a_request_does_not_depend_on_the_order_of_keys():
    one = request_digest("send_email", {"to": "ada", "cc": {"b": 1, "a": 2}})
    other = request_digest("send_email", {"cc": {"a": 2, "b": 1}, "to": "ada"})
    assert one == other


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        ("publish", {"to": "ada"}),
        ("send_email", {"to": "grace"}),
        ("send_email", {"to": "ada", "cc": None}),
        ("send_email", {"to": ["ada"]}),
        ("send_email", {"to": "ada "}),
        ("send_email", {"to": 1}),
    ],
)
def test_digest_of_a_request_differs_when_its_name_or_payload_does(name, payload):
    assert request_digest(name, payload) != request_digest("send_email", {"to": "ada"})


# ----------------------------------------------------------------- the outcome
def test_outcome_that_succeeded_has_no_error():
    with pytest.raises(ValueError, match="no error"):
        ActionOutcome(action_id="t:i", succeeded=True, result={}, error="it broke")


def test_outcome_that_failed_says_what_went_wrong():
    with pytest.raises(ValueError, match="what went wrong"):
        ActionOutcome(action_id="t:i", succeeded=False, result={}, error="")


# ----------------------------------------------------- actions that start a run
def a_run_target(**changes) -> RunTarget:
    target = {
        "workflow": "WriteContent",
        "task_queue": "openmarketer-writing",
        "kind": RunKind.WAIT,
        "longest_seconds": 600,
        "input_of": lambda child: {"run_id": str(child.run_id)},
    }
    return RunTarget(**(target | changes))


def test_run_target_of_a_waited_for_run_is_made():
    target = a_run_target()
    child = ChildRun(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), "t:1:2", {"name": "Ada"})
    assert target.kind is RunKind.WAIT
    assert target.input_of(child) == {"run_id": str(child.run_id)}
    assert target.workflow == "WriteContent"


@pytest.mark.parametrize(
    ("changed", "why"),
    [
        ({"workflow": ""}, "workflow name must not be empty"),
        ({"task_queue": ""}, "task queue of workflow 'WriteContent' must not be empty"),
        ({"kind": "wait"}, "must be a RunKind"),
        ({"longest_seconds": 0}, "longest_seconds"),
        ({"longest_seconds": float("nan")}, "longest_seconds"),
        ({"input_of": "not-a-function"}, "input_of of workflow 'WriteContent'"),
    ],
    ids=lambda case: str(case)[:40],
)
def test_run_target_that_cannot_be_one_is_refused(changed, why):
    with pytest.raises(InvalidActionDefinition, match=why):
        a_run_target(**changed)


# ------------------------------------------------------------------- the steps
def test_step_that_finished_has_an_output_and_no_action():
    outcome = StepOutcome.finished({"profile": None})
    assert (outcome.output, outcome.action) == ({"profile": None}, None)


def test_step_that_finished_with_an_empty_output_is_still_finished():
    assert StepOutcome.finished({}).output == {}


def test_step_that_waits_has_an_action_and_no_output():
    request = email()
    outcome = StepOutcome.waiting_for(request)
    assert (outcome.output, outcome.action) == (None, request)


def test_step_with_both_an_output_and_an_action_is_refused():
    with pytest.raises(ValueError, match="either"):
        StepOutcome(output={}, action=email())


def test_step_with_neither_an_output_nor_an_action_is_refused():
    with pytest.raises(ValueError, match="either"):
        StepOutcome(output=None, action=None)


# ---------------------------------------------- what a workflow decodes is typed
@pytest.mark.parametrize(
    "decoded",
    [ActionRequest, ActionOutcome, StepOutcome, steps.GraphRunRef],
    ids=lambda c: c.__name__,
)
def test_type_a_workflow_decodes_has_annotations_that_are_types_not_text(decoded):
    # The workflow sandbox's data converter evaluates text annotations in the namespace of the
    # class's module, which may not be the module the sandbox has loaded: the run then hangs.
    assert not any(isinstance(annotation, str) for annotation in decoded.__annotations__.values())


# ---------------------------------------------------------------- thread names
RUN_ID = uuid.UUID("0b7e4c1a-5f3d-4e8a-9c21-6d1f8a7b2e90")


def test_thread_of_an_analysis_run_is_the_name_the_checkpoints_are_already_stored_under():
    assert thread_id("analysis-run", RUN_ID) == "analysis-run:0b7e4c1a-5f3d-4e8a-9c21-6d1f8a7b2e90"


def test_thread_of_an_analysis_run_is_the_one_the_cleanup_looks_for():
    assert thread_id("analysis-run", RUN_ID) == run_thread_id(RUN_ID)
    assert thread_id("analysis-run", RUN_ID).startswith(ANALYSIS_RUN_THREADS)


def test_thread_of_a_kind_with_digits_and_hyphens():
    assert thread_id("post-writer-2", RUN_ID) == f"post-writer-2:{RUN_ID}"


@pytest.mark.parametrize(
    "kind",
    ["", "Analysis", "analysis_run", "analysis run", "analysis:run", "100%", "run\n", "análisis"],
)
def test_kind_with_anything_but_lower_case_letters_digits_and_hyphens_is_refused(kind):
    with pytest.raises(ValueError, match="kind of run"):
        thread_id(kind, RUN_ID)


# ------------------------------------------------- no framework in the contract
FRAMEWORK_FREE = (agent_runtime, actions, agent, gate, model_input, steps, threads, tools)
# The modules of the package, those that import LangGraph and those that do not.
LANGGRAPH_MODULES = {"langgraph_steps", "langgraph_agent"}
PLAIN_MODULES = {"__init__", "actions", "agent", "gate", "model_input", "steps", "threads", "tools"}
FRAMEWORKS = (
    "langgraph",
    "langchain",
    "temporalio",
    "sqlalchemy",
    "psycopg",
    # Core never imports an application.
    "openmarketer_worker",
    # These load LangGraph and the database driver.
    "openmarketer_core.graph_checkpoints",
    "openmarketer_core.db",
)


def imported_names(module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(inspect.getsource(module))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


@pytest.mark.parametrize("module", FRAMEWORK_FREE, ids=lambda module: module.__name__)
def test_contract_module_imports_no_framework(module):
    for name in imported_names(module):
        assert not any(name == f or name.startswith(f"{f}.") for f in FRAMEWORKS), name


def test_package_init_imports_nothing():
    assert imported_names(agent_runtime) == set()


def test_only_the_langgraph_modules_of_the_runtime_import_langgraph():
    folder = Path(agent_runtime.__file__).parent
    stems = {path.stem for path in folder.glob("*.py")}
    # A module the lists above do not know has not been looked at: add it to one of them.
    assert stems == PLAIN_MODULES | LANGGRAPH_MODULES

    importing_langgraph = {
        stem
        for stem in stems
        if any(
            name == "langgraph" or name.startswith("langgraph.")
            for name in imported_names(importlib.import_module(f"{agent_runtime.__name__}.{stem}"))
        )
    }
    assert importing_langgraph == LANGGRAPH_MODULES


def test_loading_the_contract_loads_no_framework():
    # A fresh interpreter: this one has loaded LangGraph for other tests.
    code = (
        "import sys\n"
        "import openmarketer_core.agent_runtime.actions\n"
        "import openmarketer_core.agent_runtime.agent\n"
        "import openmarketer_core.agent_runtime.model_input\n"
        "import openmarketer_core.agent_runtime.steps\n"
        "import openmarketer_core.agent_runtime.threads\n"
        "import openmarketer_core.agent_runtime.tools\n"
        "frameworks = ('langgraph', 'langchain_core', 'temporalio', 'sqlalchemy', 'psycopg',\n"
        "              'openmarketer_worker')\n"
        "loaded = [name for name in frameworks if name in sys.modules]\n"
        "assert not loaded, loaded\n"
    )
    subprocess.run([sys.executable, "-I", "-c", code], check=True)
