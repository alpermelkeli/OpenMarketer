"""Tests for what a workspace may choose about its models."""

import pytest

from openmarketer_core.llm_config import Config, ModelRouter, RoleConfig
from openmarketer_core.workspace_settings.model_choices import (
    MAX_FALLBACKS,
    ModelChoiceRefused,
    RoleChoice,
    WorkspaceModelChoices,
    acceptable_model,
    acceptable_rejected_parameters,
    acceptable_role_choice,
    role_choice_from_stored,
)

CHAT = RoleConfig(tier="fast")
EMBEDDINGS = RoleConfig(kind="embeddings", model="openai/embed")
DECISIONS = RoleConfig(kind="decisions", model="vendor/decide")


def refused_field(call) -> str:
    with pytest.raises(ModelChoiceRefused) as refusal:
        call()
    return refusal.value.field


def test_model_is_trimmed():
    assert acceptable_model("  openai/gpt-9 \n", "model") == "openai/gpt-9"


@pytest.mark.parametrize("value", ["", "gpt-9", "openai/gpt 9", "a/" + "b" * 200, None, 7])
def test_model_that_is_not_a_slug_is_refused(value):
    assert refused_field(lambda: acceptable_model(value, "model")) == "model"


def test_full_role_choice_is_accepted():
    choice = RoleChoice(
        model="openai/gpt-9",
        fallbacks=("openai/gpt-8",),
        temperature=0.7,
        max_tokens=2048,
        reasoning_effort="high",
    )
    assert acceptable_role_choice(choice, CHAT) == choice


@pytest.mark.parametrize(
    ("choice", "field"),
    [
        (RoleChoice(temperature=2.5), "temperature"),
        (RoleChoice(temperature=-0.1), "temperature"),
        (RoleChoice(temperature=float("nan")), "temperature"),
        (RoleChoice(max_tokens=0), "max_tokens"),
        (RoleChoice(max_tokens=True), "max_tokens"),
        (RoleChoice(reasoning_effort="extreme"), "reasoning_effort"),  # type: ignore[arg-type]
        (RoleChoice(fallbacks=("a/b", "a/b")), "fallbacks"),
        (RoleChoice(fallbacks=tuple(f"a/m{i}" for i in range(MAX_FALLBACKS + 1))), "fallbacks"),
        (RoleChoice(fallbacks=("not a slug",)), "fallbacks"),
    ],
)
def test_role_choice_out_of_bounds_is_refused(choice, field):
    assert refused_field(lambda: acceptable_role_choice(choice, CHAT)) == field


def test_embeddings_role_takes_a_model_only():
    assert acceptable_role_choice(RoleChoice(model="openai/e2"), EMBEDDINGS).model == "openai/e2"
    choice = RoleChoice(temperature=0.2)
    assert refused_field(lambda: acceptable_role_choice(choice, EMBEDDINGS)) == "temperature"


@pytest.mark.parametrize(
    ("choice", "field"),
    [
        (RoleChoice(fallbacks=("vendor/other",)), "fallbacks"),
        (RoleChoice(temperature=0.2), "temperature"),
        (RoleChoice(max_tokens=10), "max_tokens"),
        (RoleChoice(reasoning_effort="low"), "reasoning_effort"),
    ],
)
def test_decisions_role_takes_a_model_only(choice, field):
    assert acceptable_role_choice(RoleChoice(model="vendor/d2"), DECISIONS).model == "vendor/d2"
    with pytest.raises(ModelChoiceRefused, match="this role takes a model only") as refusal:
        acceptable_role_choice(choice, DECISIONS)
    assert refusal.value.field == field


def test_empty_choice_has_no_override():
    assert RoleChoice().is_empty
    assert RoleChoice(max_tokens=10).as_override() == {"max_tokens": 10}


def test_stored_choice_is_read_back_under_the_same_rules():
    stored = {"model": "openai/gpt-9", "fallbacks": ["openai/gpt-8"], "temperature": 0.3}
    assert role_choice_from_stored(stored, CHAT) == RoleChoice(
        model="openai/gpt-9", fallbacks=("openai/gpt-8",), temperature=0.3
    )
    assert refused_field(lambda: role_choice_from_stored({"api_key": "x"}, CHAT)) == "api_key"
    assert refused_field(lambda: role_choice_from_stored({"temperature": 9}, CHAT)) == "temperature"


def test_only_known_parameters_can_be_marked_as_rejected():
    assert acceptable_rejected_parameters(["temperature"]) == {"temperature"}
    assert refused_field(lambda: acceptable_rejected_parameters(["top_p"])) == "parameters"


def test_choices_are_laid_over_the_router_and_the_environment_still_wins():
    config = Config(
        tiers={"fast": "a/fast", "balanced": "a/balanced", "strong": "a/strong"},
        defaults={"model": "a/default"},  # type: ignore[arg-type]
        roles={"writer": RoleConfig(tier="fast"), "reader": RoleConfig(tier="fast")},
    )
    router = ModelRouter(config, env={"LLM_MODEL__READER": "env/reader"})
    choices = WorkspaceModelChoices(
        tiers={"fast": "ws/fast"},
        roles={"reader": RoleChoice(model="ws/reader", temperature=0.9)},
        rejected_parameters={"ws/fast": frozenset({"temperature"})},
    )
    chosen = choices.router_for(router)
    assert chosen.resolve("writer").model == "ws/fast"
    assert chosen.resolve("reader").model == "env/reader"
    assert chosen.resolve("reader").temperature == 0.9
    assert "temperature" not in chosen.build_chat_request("writer", [])
    assert choices.without_role("reader").router_for(router).resolve("reader").temperature == 0.4
