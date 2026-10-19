"""Tests for llm_config.py. Run: pytest config/test_llm_config.py"""
from pathlib import Path

import pytest

from llm_config import Config, ConfigError, ModelRouter
from openmarketer_core.llm_config import CapabilityProblem, CatalogModel, ModelProblem

YAML = Path(__file__).with_name("models.yaml")
BASE_ENV = {"OPENROUTER_API_KEY": "test-key"}


def router(env=None, **kw):
    return ModelRouter.from_file(YAML, env={**BASE_ENV, **(env or {})}, **kw)


def test_yaml_loads_and_every_role_resolves():
    r = router()
    resolved = r.resolve_all()
    assert {"orchestrator", "reader", "repo_analyzer", "writer", "planner", "embeddings"} <= set(resolved)
    assert all(x.model for x in resolved.values())


def test_tier_is_used_when_role_has_no_explicit_model():
    r = router().resolve("writer")
    assert r.source == "tier:balanced"
    assert r.model == router().config.tiers["balanced"]


def test_tier_env_override_changes_every_role_in_the_tier():
    r = router({"LLM_MODEL_BALANCED": "openai/test-model"})
    assert r.resolve("writer").model == "openai/test-model"
    assert r.resolve("analyst").model == "openai/test-model"
    assert r.resolve("writer").source == "env-tier:balanced"
    assert r.resolve("reader").model != "openai/test-model"  # fast tier is untouched


def test_precedence_env_over_project_over_workspace_over_yaml():
    ws = {"writer": {"model": "google/ws-model"}}
    pr = {"writer": {"model": "google/pr-model"}}
    assert router(workspace_overrides=ws).resolve("writer").source == "workspace"
    assert router(workspace_overrides=ws, project_overrides=pr).resolve("writer").model == "google/pr-model"
    r = router({"LLM_MODEL__WRITER": "mistralai/env-model"}, workspace_overrides=ws, project_overrides=pr)
    assert r.resolve("writer").model == "mistralai/env-model"
    assert r.resolve("writer").source == "env"


def test_parameter_overrides_apply_per_role():
    r = router(project_overrides={"writer": {"temperature": 1.1, "max_tokens": 900}}).resolve("writer")
    assert (r.temperature, r.max_tokens) == (1.1, 900)
    assert router().resolve("writer").temperature == 0.8  # role value from yaml


def test_fallbacks_never_include_the_primary_model():
    r = router({"LLM_MODEL__READER": "anthropic/claude-haiku-4.5"}).resolve("reader")
    assert "anthropic/claude-haiku-4.5" not in r.fallbacks


def test_invalid_slug_and_unknown_role_are_rejected():
    with pytest.raises(ConfigError):
        router({"LLM_MODEL__WRITER": "not-a-slug"}).resolve("writer")
    with pytest.raises(ConfigError):
        router(project_overrides={"nope": {"model": "a/b"}})
    with pytest.raises(ConfigError):
        router(project_overrides={"writer": {"api_key": "x"}})
    with pytest.raises(KeyError):
        router().resolve("nope")


def test_reader_role_cannot_receive_tools():
    r = router()
    msgs = [{"role": "user", "content": "classify this"}]
    with pytest.raises(ConfigError):
        r.build_chat_request("reader", msgs, tools=[{"type": "function", "function": {"name": "x"}}])
    # roles that declare 'tools' may use them
    payload = r.build_chat_request("orchestrator", msgs, tools=[{"type": "function", "function": {"name": "get_status"}}])
    assert payload["tools"]


def test_request_payload_shape_for_openrouter():
    r = router()
    p = r.build_chat_request("writer", [{"role": "user", "content": "hi"}])
    assert p["model"] == r.resolve("writer").model
    assert p["models"][0] == p["model"] and len(p["models"]) >= 2
    assert p["provider"] == {"allow_fallbacks": True, "require_parameters": True, "data_collection": "deny"}
    h = r.headers()
    assert h["Authorization"] == "Bearer test-key" and h["X-Title"] == "OpenMarketer"


def test_temperature_is_left_out_for_a_model_that_rejects_it():
    msgs = [{"role": "user", "content": "hi"}]
    rejecting = router({"LLM_MODEL__WRITER": "anthropic/claude-sonnet-5.5"})
    assert "temperature" not in rejecting.build_chat_request("writer", msgs)
    accepting = router({"LLM_MODEL__WRITER": "anthropic/claude-haiku-4.5"})
    assert accepting.build_chat_request("writer", msgs)["temperature"] == 0.8


def test_missing_api_key_is_a_clear_error():
    with pytest.raises(ConfigError, match="OPENROUTER_API_KEY"):
        ModelRouter.from_file(YAML, env={}).headers()


def test_embeddings_role_is_not_a_chat_role():
    with pytest.raises(ConfigError):
        router().build_chat_request("embeddings", [])


def catalog_entry(slug, parameters, modalities=("text",)):
    return CatalogModel(
        id=slug,
        name=slug,
        context_length=None,
        prompt_price=None,
        completion_price=None,
        supported_parameters=frozenset(parameters),
        input_modalities=frozenset(modalities),
    )


CATALOG = {
    "anthropic/claude-sonnet-5.5": catalog_entry(
        "anthropic/claude-sonnet-5.5", ["tools", "structured_outputs"], ["text", "image"]
    ),
    "anthropic/claude-haiku-4.5": catalog_entry("anthropic/claude-haiku-4.5", ["temperature"]),
}


def only(r, role, model):
    return r.resolve(role).model_copy(update={"model": model, "fallbacks": []})


def test_capability_check_reports_missing_features():
    r = router()
    assert r.check_capabilities(only(r, "orchestrator", "anthropic/claude-sonnet-5.5"), CATALOG) == []
    bad = r.check_capabilities(only(r, "vision", "anthropic/claude-haiku-4.5"), CATALOG)
    assert ModelProblem("anthropic/claude-haiku-4.5", CapabilityProblem.NO_IMAGE_INPUT) in bad
    unknown = r.check_capabilities(only(r, "writer", "x/y"), CATALOG)
    assert unknown == [ModelProblem("x/y", CapabilityProblem.NOT_IN_CATALOG)]


def test_capability_check_reports_temperature_only_when_the_request_carries_it():
    catalog = {"openai/o9": catalog_entry("openai/o9", [])}
    sent = router().check_capabilities(only(router(), "writer", "openai/o9"), catalog)
    assert sent == [ModelProblem("openai/o9", CapabilityProblem.REJECTS_TEMPERATURE)]
    marked = router(workspace_rejected_parameters={"openai/o9": ["temperature"]})
    assert marked.check_capabilities(only(marked, "writer", "openai/o9"), catalog) == []


def test_capability_check_tells_a_fallback_that_rejects_temperature_apart():
    catalog = {
        "openai/o9": catalog_entry("openai/o9", []),
        "openai/warm": catalog_entry("openai/warm", ["temperature"]),
    }
    r = router()
    resolved = r.resolve("writer").model_copy(
        update={"model": "openai/warm", "fallbacks": ["openai/o9"]}
    )
    assert r.check_capabilities(resolved, catalog) == [
        ModelProblem("openai/o9", CapabilityProblem.FALLBACK_REJECTS_TEMPERATURE)
    ]


def test_workspace_tier_is_used_unless_the_environment_sets_the_tier():
    chosen = router(workspace_tiers={"balanced": "openai/ws-tier"})
    assert chosen.resolve("writer").model == "openai/ws-tier"
    assert chosen.resolve("writer").source == "workspace-tier:balanced"
    from_env = router({"LLM_MODEL_BALANCED": "openai/env-tier"}, workspace_tiers={"balanced": "openai/ws-tier"})
    assert from_env.resolve("writer").model == "openai/env-tier"


def test_workspace_role_choice_wins_over_the_workspace_tier():
    r = router(
        workspace_overrides={"writer": {"model": "google/ws-role"}},
        workspace_tiers={"balanced": "openai/ws-tier"},
    )
    assert r.resolve("writer").model == "google/ws-role"


def test_temperature_is_left_out_for_a_model_the_workspace_marked():
    msgs = [{"role": "user", "content": "hi"}]
    r = router(
        {"LLM_MODEL__WRITER": "anthropic/claude-haiku-4.5"},
        workspace_rejected_parameters={"anthropic/claude-haiku-4.5": ["temperature"]},
    )
    assert "temperature" not in r.build_chat_request("writer", msgs)


def test_workspace_choices_for_unknown_tiers_or_parameters_are_rejected():
    with pytest.raises(ConfigError):
        router(workspace_tiers={"huge": "a/b"})
    with pytest.raises(ConfigError):
        router(workspace_rejected_parameters={"a/b": ["top_p"]})


def test_with_workspace_keeps_the_environment_and_replaces_the_workspace_layers():
    base = router({"LLM_MODEL__READER": "openai/env-reader"})
    r = base.with_workspace(
        overrides={"writer": {"model": "google/ws-role"}}, tiers={}, rejected_parameters={}
    )
    assert r.resolve("reader").model == "openai/env-reader"
    assert r.resolve("writer").model == "google/ws-role"
    assert base.resolve("writer").source == "tier:balanced"


def test_decisions_request_carries_the_model_the_state_and_the_questions_only():
    question = {
        "type": "noul",
        "instructions": "Is it?",
        "criteria": {"true": "yes", "false": "no"},
    }
    payload = router().build_decision_request("screener", ["a text"], {"r0": question})
    assert payload == {
        "model": router().resolve("screener").model,
        "state": ["a text"],
        "questions": {"r0": question},
    }


def test_decisions_role_model_is_resolved_like_any_role():
    r = router({"LLM_MODEL__SCREENER": "vendor/other-decider"})
    assert r.build_decision_request("screener", [], {})["model"] == "vendor/other-decider"
    assert r.resolve("screener").fallbacks == []


def test_decisions_and_chat_roles_do_not_build_each_others_requests():
    with pytest.raises(ConfigError, match="not 'chat'"):
        router().build_chat_request("screener", [])
    with pytest.raises(ConfigError, match="not 'decisions'"):
        router().build_decision_request("writer", [], {})


def test_decisions_role_is_not_checked_against_the_chat_catalogue():
    r = router()
    assert r.check_capabilities(r.resolve("screener"), CATALOG) == []


@pytest.mark.parametrize(
    "role",
    [
        {"kind": "decisions"},
        {"kind": "decisions", "model": "vendor/decide", "temperature": 0.0},
        {"kind": "decisions", "model": "vendor/decide", "tier": "fast"},
        {"kind": "decisions", "model": "vendor/decide", "fallbacks": ["vendor/other"]},
        {"kind": "decisions", "model": "vendor/decide", "requires": ["tools"]},
    ],
    ids=["no-model", "temperature", "tier", "fallbacks", "requires"],
)
def test_decisions_role_names_a_model_and_takes_nothing_else(role):
    with pytest.raises(ValueError, match="roles.screener"):
        Config.model_validate(
            {
                "tiers": {"fast": "a/f", "balanced": "a/b", "strong": "a/s"},
                "defaults": {"model": "a/d"},
                "roles": {"screener": role},
            }
        )


def test_images_request_asks_the_roles_model_for_an_image_and_nothing_of_a_chat():
    msgs = [{"role": "user", "content": "draw"}]
    payload = router().build_image_request(
        "image_generator", msgs, image_config={"aspect_ratio": "16:9"}
    )
    assert payload["model"] == router().resolve("image_generator").model
    assert payload["modalities"] == ["image", "text"]
    assert payload["image_config"] == {"aspect_ratio": "16:9"}
    assert payload["usage"] == {"include": True}
    assert not {"temperature", "max_tokens", "reasoning"} & set(payload)


def test_images_request_offers_the_fallbacks_after_the_model_in_order():
    r = router()
    resolved = r.resolve("image_generator")
    payload = r.build_image_request("image_generator", [])
    assert resolved.fallbacks
    assert payload["models"] == [resolved.model, *resolved.fallbacks]


def test_images_request_without_fallbacks_names_one_model():
    r = router().with_workspace(
        overrides={"image_generator": {"fallbacks": []}}, tiers={}, rejected_parameters={}
    )
    assert "models" not in r.build_image_request("image_generator", [])


def test_images_and_chat_roles_do_not_build_each_others_requests():
    with pytest.raises(ConfigError, match="not 'chat'"):
        router().build_chat_request("image_generator", [])
    with pytest.raises(ConfigError, match="not 'images'"):
        router().build_image_request("writer", [])


def test_images_role_has_its_own_fallbacks_and_keeps_its_estimate():
    resolved = router().resolve("image_generator")
    assert resolved.kind == "images" and resolved.fallbacks == ["google/gemini-3.1-flash-lite-image"]
    assert resolved.estimate_usd_per_image is not None


def test_images_role_never_takes_the_defaults_chat_fallbacks():
    config = Config.model_validate(
        {
            "tiers": {"fast": "a/f", "balanced": "a/b", "strong": "a/s"},
            "defaults": {"model": "a/d", "fallbacks": ["a/chat"]},
            "roles": {
                "drawer": {"kind": "images", "model": "v/draw", "requires": ["image_output"]}
            },
        }
    )
    assert ModelRouter(config, env=BASE_ENV).resolve("drawer").fallbacks == []


def test_capability_check_reports_a_model_that_does_not_make_images():
    drawer = CatalogModel(
        id="google/drawer",
        name="drawer",
        context_length=None,
        prompt_price=None,
        completion_price=None,
        supported_parameters=frozenset(),
        input_modalities=frozenset({"text", "image"}),
        output_modalities=frozenset({"text", "image"}),
    )
    talker = catalog_entry("google/talker", [], ["text", "image"])
    catalog = {"google/drawer": drawer, "google/talker": talker}
    r = router()
    assert r.check_capabilities(only(r, "image_generator", "google/drawer"), catalog) == []
    assert r.check_capabilities(only(r, "image_generator", "google/talker"), catalog) == [
        ModelProblem("google/talker", CapabilityProblem.NO_IMAGE_OUTPUT)
    ]
    with_fallback = r.resolve("image_generator").model_copy(
        update={"model": "google/drawer", "fallbacks": ["google/talker"]}
    )
    assert r.check_capabilities(with_fallback, catalog) == [
        ModelProblem("google/talker", CapabilityProblem.NO_IMAGE_OUTPUT)
    ]


@pytest.mark.parametrize(
    "role",
    [
        {"kind": "images", "requires": ["image_output"]},
        {"kind": "images", "model": "vendor/draw"},
        {"kind": "images", "model": "vendor/draw", "requires": ["image_output"], "tier": "fast"},
        {"kind": "images", "model": "vendor/draw", "requires": ["image_output"], "temperature": 1},
        {"kind": "images", "model": "vendor/draw", "requires": ["image_output"], "fallbacks": ["?"]},
        {"kind": "chat", "requires": ["image_output"]},
        {"kind": "chat", "estimate_usd_per_image": 0.1},
        {"kind": "images", "model": "v/d", "requires": ["image_output"], "estimate_usd_per_image": -1},
    ],
    ids=[
        "no-model",
        "no-image-output",
        "tier",
        "temperature",
        "fallback-not-a-model",
        "chat-with-image-output",
        "chat-with-estimate",
        "negative-estimate",
    ],
)
def test_images_role_names_a_model_that_draws_and_takes_nothing_of_a_chat(role):
    with pytest.raises(ValueError, match="roles.drawer"):
        Config.model_validate(
            {
                "tiers": {"fast": "a/f", "balanced": "a/b", "strong": "a/s"},
                "defaults": {"model": "a/d"},
                "roles": {"drawer": role},
            }
        )
