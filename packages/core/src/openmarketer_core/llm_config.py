"""OpenMarketer LLM model configuration and per-role model resolution.

Every LLM call site in the system is a *role* (writer, reader, planner, ...).
This module loads ``models.yaml`` and resolves the model for a role in this
order (first match wins):

    1. environment variable   LLM_MODEL__<ROLE>
    2. project override       (dashboard settings, stored in the database)
    3. workspace override     (dashboard settings, stored in the database)
    4. models.yaml            roles.<role>.model
    5. the role's tier        LLM_MODEL_<TIER>, then the workspace's choice for the tier
                              (dashboard settings), then models.yaml tiers.<tier>
    6. models.yaml            defaults.model

A role is of one of four kinds: ``chat`` (a conversation, the most of them),
``embeddings``, ``decisions``: yes-or-no questions about a list of texts,
answered with a probability each by OpenRouter's decisions endpoint
(``provider.decisions_url``), or ``images``: a request to the chat endpoint
that asks for an image back (``build_image_request``). A decisions role names
its model and takes nothing else: none of a chat request's parameters applies
to it. An images role names its model, requires image output, may give
``fallbacks`` (models that also draw, which the caller tries in order, one
request each, when the first fails in a way another model may pass; the
defaults' chat fallbacks never apply to it) and ``estimate_usd_per_image``, what
one image is reckoned to cost when the catalogue gives no price per image.

It also builds OpenRouter chat-completion requests and decision requests.
OpenRouter exposes an OpenAI-compatible chat API, so the same chat payload also
works with other compatible endpoints by changing ``base_url``. A parameter a
model's providers reject is left out of its requests: those listed in models.yaml
and those a workspace marked from the dashboard.

What the router knows of the dashboard's settings is handed to it as plain
mappings (``with_workspace``); it reads no database. Whether a model has what a
role needs is checked against the provider's catalogue (``check_capabilities``),
which the caller fetches (``llm_catalog.py``).

Usage:
    python config/llm_config.py show               # table of role -> model + source
    python config/llm_config.py show --config config/models.yaml

Dependencies: pyyaml, pydantic>=2.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import urllib.request
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DEFAULT_CONFIG_PATH = "config/models.yaml"  # relative to the repository root

Capability = Literal["tools", "structured_output", "vision", "image_output"]
Tier = Literal["fast", "balanced", "strong"]
RoleKind = Literal["chat", "embeddings", "decisions", "images"]
# The request parameters a model can be marked as rejecting. Only one so far.
RejectableParameter = Literal["temperature"]
REJECTABLE_PARAMETERS: tuple[RejectableParameter, ...] = ("temperature",)

# OpenRouter slugs look like "provider/model" with an optional ":variant" suffix.
SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._:+/-]+$")
ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(ValueError):
    """Raised for invalid configuration or invalid overrides."""


def _check_slug(value: str, where: str) -> str:
    if not SLUG_RE.match(value):
        raise ConfigError(f"{where}: '{value}' is not a valid 'provider/model' slug")
    return value


def _interpolate(obj: Any, env: Mapping[str, str]) -> Any:
    """Expand ${VAR} and ${VAR:-default} in every string of a loaded YAML tree."""
    if isinstance(obj, str):
        return ENV_RE.sub(lambda m: env.get(m.group(1)) or (m.group(2) or ""), obj)
    if isinstance(obj, list):
        return [_interpolate(x, env) for x in obj]
    if isinstance(obj, dict):
        return {k: _interpolate(v, env) for k, v in obj.items()}
    return obj


# ----------------------------------------------------------------- schema
class Routing(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allow_fallbacks: bool = True
    require_parameters: bool = True
    data_collection: Literal["allow", "deny"] = "deny"


class Provider(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = "openrouter"
    base_url: str = "https://openrouter.ai/api/v1"
    # OpenRouter's decisions endpoint, in alpha: yes-or-no questions answered with probabilities.
    decisions_url: str = "https://openrouter.ai/api/alpha/decisions"
    api_key_env: str = "OPENROUTER_API_KEY"
    app_url: str = ""
    app_name: str = "OpenMarketer"
    routing: Routing = Routing()


class Defaults(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str
    temperature: float = 0.4
    max_tokens: int = 4096
    timeout_s: int = 120
    fallbacks: list[str] = Field(default_factory=list)

    @field_validator("model")
    @classmethod
    def _slug(cls, v: str) -> str:
        return _check_slug(v, "defaults.model")

    @field_validator("fallbacks")
    @classmethod
    def _slugs(cls, v: list[str]) -> list[str]:
        return [_check_slug(x, "defaults.fallbacks") for x in v]


class Limits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_cost_per_call_usd: float = 1.5


# What a decisions role may not set: each is a part of a chat request (or of an embeddings one).
DECISIONS_ROLE_REFUSES = (
    "tier",
    "temperature",
    "max_tokens",
    "fallbacks",
    "requires",
    "reasoning_effort",
    "dimensions",
)
# What an images role may not set: the parts of a chat request it does not send. A model of its
# own is required too, since the tiers are chat models. Its fallbacks are sent as a chat's are.
IMAGES_ROLE_REFUSES = (
    "tier",
    "temperature",
    "max_tokens",
    "reasoning_effort",
    "dimensions",
)


class RoleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: RoleKind = "chat"
    model: str | None = None
    tier: Tier | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    fallbacks: list[str] | None = None
    requires: list[Capability] = Field(default_factory=list)
    reasoning_effort: Literal["low", "medium", "high"] | None = None
    dimensions: int | None = None
    estimate_usd_per_image: float | None = Field(default=None, ge=0)

    @field_validator("model")
    @classmethod
    def _slug(cls, v: str | None) -> str | None:
        return v if v is None else _check_slug(v, "role.model")

    @field_validator("fallbacks")
    @classmethod
    def _slugs(cls, v: list[str] | None) -> list[str] | None:
        return v if v is None else [_check_slug(x, "role.fallbacks") for x in v]

    @model_validator(mode="after")
    def _decisions_take_a_model_only(self) -> RoleConfig:
        """A decisions request carries a model, a state and questions; nothing else applies."""
        if self.kind != "decisions":
            return self
        if self.model is None:
            raise ValueError("a decisions role names its model")
        others = [part for part in DECISIONS_ROLE_REFUSES if getattr(self, part) not in (None, [])]
        if others:
            raise ValueError(f"a decisions role takes a model only, not {', '.join(others)}")
        return self

    @model_validator(mode="after")
    def _images_name_a_model_that_draws(self) -> RoleConfig:
        """An images request carries its models, messages and the shape; nothing else of a chat."""
        if self.kind != "images":
            if self.estimate_usd_per_image is not None or "image_output" in self.requires:
                raise ValueError("only an images role makes images")
            return self
        if self.model is None:
            raise ValueError("an images role names its model")
        if "image_output" not in self.requires:
            raise ValueError("an images role requires image_output")
        others = [part for part in IMAGES_ROLE_REFUSES if getattr(self, part) not in (None, [])]
        if others:
            raise ValueError(f"an images role does not take {', '.join(others)}")
        return self


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = 1
    provider: Provider = Provider()
    tiers: dict[Tier, str]
    defaults: Defaults
    limits: Limits = Limits()
    roles: dict[str, RoleConfig]
    # With routing.require_parameters on, a request carrying a parameter that none of a
    # model's providers accept finds no endpoint (OpenRouter answers 404), so a parameter
    # listed here for a model is left out of its requests.
    unsupported_parameters: dict[str, list[RejectableParameter]] = Field(default_factory=dict)

    @field_validator("tiers")
    @classmethod
    def _tier_slugs(cls, v: dict[str, str]) -> dict[str, str]:
        return {k: _check_slug(s, f"tiers.{k}") for k, s in v.items()}

    @field_validator("unsupported_parameters")
    @classmethod
    def _unsupported_slugs(cls, v: dict[str, list[str]]) -> dict[str, list[str]]:
        for slug in v:
            _check_slug(slug, "unsupported_parameters")
        return v


class Resolved(BaseModel):
    """The effective settings for one role after applying every override."""

    role: str
    kind: str
    model: str
    source: str
    fallbacks: list[str]
    temperature: float
    max_tokens: int
    timeout_s: int
    requires: list[str]
    reasoning_effort: str | None = None
    dimensions: int | None = None
    estimate_usd_per_image: float | None = None

    @property
    def tools_allowed(self) -> bool:
        """Tools are available only to roles that declare the 'tools' capability."""
        return "tools" in self.requires


@dataclass(frozen=True)
class CatalogModel:
    """What the provider's catalogue says of one model, as far as a role's needs go.

    Prices are in US dollars per million tokens, ``None`` when the catalogue gives none.
    """

    id: str
    name: str
    context_length: int | None
    prompt_price: float | None
    completion_price: float | None
    supported_parameters: frozenset[str]
    input_modalities: frozenset[str]
    output_modalities: frozenset[str] = frozenset()
    # US dollars per million output tokens of an image; how many tokens an image is, is the model's.
    image_output_price: float | None = None
    # OpenRouter's ``pricing.image`` as it gives it, in US dollars per image. Its documentation
    # does not say whether that is an image read or an image made (vision models carry it too),
    # so it is shown as it is and never used for an estimate.
    image_price: float | None = None


class CapabilityProblem(StrEnum):
    """Why a model may not serve a role."""

    NOT_IN_CATALOG = "not_in_catalog"
    NO_TOOLS = "no_tools"
    NO_STRUCTURED_OUTPUT = "no_structured_output"
    NO_IMAGE_INPUT = "no_image_input"
    NO_IMAGE_OUTPUT = "no_image_output"
    REJECTS_TEMPERATURE = "rejects_temperature"
    # A fallback is sent the request made for the first model, temperature included.
    FALLBACK_REJECTS_TEMPERATURE = "fallback_rejects_temperature"


@dataclass(frozen=True)
class ModelProblem:
    """One model of a role's request, and what it lacks for that role."""

    model: str
    problem: CapabilityProblem


IMAGE_ONLY = ["image"]
IMAGE_AND_TEXT = ["image", "text"]


def image_modalities(model: str, catalog: Mapping[str, CatalogModel] | None) -> list[str]:
    """What an image request to ``model`` asks back: its own outputs, as the catalogue gives them.

    OpenRouter's image-generation guide asks for ``["image", "text"]`` from a model that
    answers with both (Gemini, GPT-5 Image) and ``["image"]`` from one that makes images only
    (FLUX, Seedream, Recraft), and a model of images only refuses the first. It does not say
    that a model of both accepts ``["image"]``, so ``["image"]`` is asked only when the
    catalogue says the model makes images and no text. A model the catalogue does not list, or
    one whose outputs it does not give, or no catalogue at all: ``["image", "text"]``, which
    the caller may ask again as ``["image"]`` when the provider says no endpoint gives those
    outputs.
    """
    entry = catalog.get(model) if catalog is not None else None
    outputs = entry.output_modalities if entry is not None else frozenset()
    if "image" in outputs and "text" not in outputs:
        return list(IMAGE_ONLY)
    return list(IMAGE_AND_TEXT)


# ----------------------------------------------------------------- router
class ModelRouter:
    """Resolve per-role model settings and build OpenRouter requests."""

    OVERRIDABLE = {"model", "temperature", "max_tokens", "fallbacks", "reasoning_effort"}

    def __init__(
        self,
        config: Config,
        env: Mapping[str, str] | None = None,
        workspace_overrides: dict[str, dict[str, Any]] | None = None,
        project_overrides: dict[str, dict[str, Any]] | None = None,
        workspace_tiers: Mapping[str, str] | None = None,
        workspace_rejected_parameters: Mapping[str, Collection[str]] | None = None,
    ) -> None:
        """``workspace_tiers`` maps a tier to the model the workspace chose for it, and
        ``workspace_rejected_parameters`` a model to the parameters the workspace marked it as
        rejecting; both come from the dashboard, as the two override layers do.
        """
        self.config = config
        self.env = dict(os.environ if env is None else env)
        self.workspace = workspace_overrides or {}
        self.project = project_overrides or {}
        self.workspace_tiers = dict(workspace_tiers or {})
        self.workspace_rejected_parameters = {
            model: frozenset(parameters)
            for model, parameters in (workspace_rejected_parameters or {}).items()
        }
        for scope, data in (("workspace", self.workspace), ("project", self.project)):
            for role, values in data.items():
                self._validate_override(scope, role, values)
        for tier, model in self.workspace_tiers.items():
            if tier not in self.config.tiers:
                raise ConfigError(f"workspace choice for unknown tier '{tier}'")
            _check_slug(model, f"workspace choice for tier '{tier}'")
        for model, parameters in self.workspace_rejected_parameters.items():
            _check_slug(model, "workspace rejected parameters")
            unknown = set(parameters) - set(REJECTABLE_PARAMETERS)
            if unknown:
                raise ConfigError(f"workspace rejected parameters: unknown {sorted(unknown)}")

    def with_workspace(
        self,
        *,
        overrides: dict[str, dict[str, Any]],
        tiers: Mapping[str, str],
        rejected_parameters: Mapping[str, Collection[str]],
    ) -> ModelRouter:
        """This router with a workspace's choices from the dashboard in place of its own."""
        return ModelRouter(
            self.config,
            env=self.env,
            workspace_overrides=overrides,
            project_overrides=self.project,
            workspace_tiers=tiers,
            workspace_rejected_parameters=rejected_parameters,
        )

    # -- loading -------------------------------------------------------
    @classmethod
    def from_file(cls, path: str | Path, **kwargs: Any) -> ModelRouter:
        env = dict(os.environ if kwargs.get("env") is None else kwargs["env"])
        kwargs["env"] = env
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return cls(Config.model_validate(_interpolate(raw, env)), **kwargs)

    # -- validation ----------------------------------------------------
    def _validate_override(self, scope: str, role: str, values: dict[str, Any]) -> None:
        if role not in self.config.roles:
            raise ConfigError(f"{scope} override for unknown role '{role}'")
        unknown = set(values) - self.OVERRIDABLE
        if unknown:
            raise ConfigError(f"{scope} override for '{role}': unsupported keys {sorted(unknown)}")
        if "model" in values:
            _check_slug(values["model"], f"{scope} override for '{role}'")
        for fb in values.get("fallbacks", []) or []:
            _check_slug(fb, f"{scope} override for '{role}' fallbacks")

    # -- resolution ----------------------------------------------------
    def tier_model(self, tier: str) -> tuple[str, str]:
        """The model of ``tier`` and where it comes from: environment, workspace or file."""
        override = self.env.get(f"LLM_MODEL_{tier.upper()}", "").strip()
        if override:
            return _check_slug(override, f"LLM_MODEL_{tier.upper()}"), f"env-tier:{tier}"
        if tier in self.workspace_tiers:
            return self.workspace_tiers[tier], f"workspace-tier:{tier}"
        return self.config.tiers[tier], f"tier:{tier}"  # type: ignore[index]

    def rejected_parameters(self, model: str) -> frozenset[str]:
        """The parameters left out of a request to ``model``: the file's and the workspace's."""
        from_file = self.config.unsupported_parameters.get(model, [])
        return frozenset(from_file) | self.workspace_rejected_parameters.get(model, frozenset())

    def resolve(self, role: str) -> Resolved:
        if role not in self.config.roles:
            raise KeyError(f"unknown role '{role}'; known roles: {sorted(self.config.roles)}")
        rc, d = self.config.roles[role], self.config.defaults

        # model
        env_model = self.env.get(f"LLM_MODEL__{role.upper()}", "").strip()
        if env_model:
            model, source = _check_slug(env_model, f"LLM_MODEL__{role.upper()}"), "env"
        elif "model" in self.project.get(role, {}):
            model, source = self.project[role]["model"], "project"
        elif "model" in self.workspace.get(role, {}):
            model, source = self.workspace[role]["model"], "workspace"
        elif rc.model:
            model, source = rc.model, "role"
        elif rc.tier:
            model, source = self.tier_model(rc.tier)
        else:
            model, source = d.model, "default"

        # other parameters: project > workspace > role > defaults
        def pick(key: str, default: Any) -> Any:
            for layer in (self.project.get(role, {}), self.workspace.get(role, {})):
                if layer.get(key) is not None:
                    return layer[key]
            value = getattr(rc, key, None)
            return default if value is None else value

        # A decisions request has no fallbacks: the endpoint takes one model. An images role's
        # are its own or its workspace's only: the defaults' are chat models, which do not draw.
        if rc.kind == "decisions":
            fallbacks: list[str] = []
        else:
            chosen = pick("fallbacks", [] if rc.kind == "images" else d.fallbacks)
            fallbacks = [m for m in chosen if m != model]
        return Resolved(
            role=role,
            kind=rc.kind,
            model=model,
            source=source,
            fallbacks=fallbacks,
            temperature=pick("temperature", d.temperature),
            max_tokens=pick("max_tokens", d.max_tokens),
            timeout_s=d.timeout_s,
            requires=list(rc.requires),
            reasoning_effort=pick("reasoning_effort", None),
            dimensions=rc.dimensions,
            estimate_usd_per_image=rc.estimate_usd_per_image,
        )

    def resolve_all(self) -> dict[str, Resolved]:
        return {r: self.resolve(r) for r in self.config.roles}

    # -- capability check ---------------------------------------------
    def check_capabilities(
        self, resolved: Resolved, catalog: Mapping[str, CatalogModel]
    ) -> list[ModelProblem]:
        """What each model of the role's request lacks for the role, given the provider's catalogue.

        Every model of the request is checked, the fallbacks too: they are sent the same
        request. A model the catalogue does not list is reported, not guessed. Temperature
        is a problem only when the request carries it, which the first model decides; a
        fallback that rejects it is reported as such, since marking it would not help.

        A decisions role is not checked: the catalogue lists chat models only, and the
        decisions endpoint's models are not in it, so every one would be reported as unknown.
        Nothing a decisions role requires could be read from the catalogue either; the
        endpoint refuses a model it does not serve, and the call says so.
        """
        if resolved.kind == "decisions":
            return []
        rejected = self.rejected_parameters(resolved.model)
        sends_temperature = resolved.kind == "chat" and "temperature" not in rejected
        problems: list[ModelProblem] = []
        for position, slug in enumerate([resolved.model, *resolved.fallbacks]):
            entry = catalog.get(slug)
            if entry is None:
                problems.append(ModelProblem(slug, CapabilityProblem.NOT_IN_CATALOG))
                continue
            params = entry.supported_parameters
            if "tools" in resolved.requires and "tools" not in params:
                problems.append(ModelProblem(slug, CapabilityProblem.NO_TOOLS))
            if "structured_output" in resolved.requires and not params & {
                "structured_outputs",
                "response_format",
            }:
                problems.append(ModelProblem(slug, CapabilityProblem.NO_STRUCTURED_OUTPUT))
            if "vision" in resolved.requires and "image" not in entry.input_modalities:
                problems.append(ModelProblem(slug, CapabilityProblem.NO_IMAGE_INPUT))
            if "image_output" in resolved.requires and "image" not in entry.output_modalities:
                problems.append(ModelProblem(slug, CapabilityProblem.NO_IMAGE_OUTPUT))
            if sends_temperature and "temperature" not in params:
                problem = (
                    CapabilityProblem.REJECTS_TEMPERATURE
                    if position == 0
                    else CapabilityProblem.FALLBACK_REJECTS_TEMPERATURE
                )
                problems.append(ModelProblem(slug, problem))
        return problems

    # -- requests ------------------------------------------------------
    def api_key_in_environment(self) -> str | None:
        """The provider's key in this router's environment, or ``None`` when it is not set there.

        The variable is the one the configuration names (``provider.api_key_env``). Its
        value is given without the whitespace around it; one that is empty or only
        whitespace is not set.
        """
        return self.env.get(self.config.provider.api_key_env, "").strip() or None

    def headers(self, api_key: str | None = None) -> dict[str, str]:
        """The headers of a request to the provider, with ``api_key`` as its key.

        Without ``api_key`` the key is the environment's, which is all the command line and
        the benchmark have. A caller that keeps keys elsewhere (the worker, for a workspace's
        stored one) hands in the key to use; this module reads no database. Raises
        ``ConfigError``, which names the variable and never a key, when there is neither.
        """
        p = self.config.provider
        key = api_key or self.api_key_in_environment()
        if not key:
            raise ConfigError(f"environment variable {p.api_key_env} is not set")
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        if p.app_url:
            headers["HTTP-Referer"] = p.app_url
        if p.app_name:
            headers["X-Title"] = p.app_name
        return headers

    def build_chat_request(
        self,
        role: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        r = self.resolve(role)
        if r.kind != "chat":
            raise ConfigError(f"role '{role}' is of kind '{r.kind}', not 'chat'")
        if tools and not r.tools_allowed:
            raise ConfigError(f"role '{role}' is not allowed to use tools")
        routing = self.config.provider.routing
        payload: dict[str, Any] = {
            "model": r.model,
            "messages": messages,
            "max_tokens": r.max_tokens,
            "provider": routing.model_dump(),
        }
        if "temperature" not in self.rejected_parameters(r.model):
            payload["temperature"] = r.temperature
        if r.fallbacks:
            payload["models"] = [r.model, *r.fallbacks]  # OpenRouter tries these in order
        if r.reasoning_effort:
            payload["reasoning"] = {"effort": r.reasoning_effort}
        if tools:
            payload["tools"] = tools
        if response_format:
            payload["response_format"] = response_format
        if extra:
            payload.update(extra)
        return payload

    def image_models(self, role: str) -> list[str]:
        """The models an image request of ``role`` tries, in order: its model, its fallbacks."""
        r = self.resolve(role)
        return [r.model, *r.fallbacks]

    def build_image_request(
        self,
        role: str,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        image_config: Mapping[str, Any] | None = None,
        catalog: Mapping[str, CatalogModel] | None = None,
        modalities: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """The body of a chat-completions request to one model of ``role`` that asks for an
        image back.

        ``model`` is the role's model (the default) or one of its fallbacks: a request names
        one model and no ``models`` list, because OpenRouter, given the list, answered from the
        fallback although the first model answers alone, and a list is sent one ``modalities``
        that a model of images only refuses. The caller tries the fallbacks itself
        (``image_models``). ``image_config`` is OpenRouter's description of the image (its
        ``aspect_ratio``); a model that does not read it ignores it, and the caller fits what
        comes back. The provider routing is the chat requests', and so is the cost the reply
        reports (``usage``). ``modalities`` are the model's own (``image_modalities`` of
        ``catalog``, the provider's when the caller could read it) unless given. Raises
        ``ConfigError`` for a role that is not of kind ``images`` and for a model that is not
        the role's.
        """
        r = self.resolve(role)
        if r.kind != "images":
            raise ConfigError(f"role '{role}' is of kind '{r.kind}', not 'images'")
        chosen = r.model if model is None else model
        if chosen not in (r.model, *r.fallbacks):
            raise ConfigError(f"role '{role}' does not name that model")
        payload: dict[str, Any] = {
            "model": chosen,
            "messages": messages,
            "modalities": list(modalities)
            if modalities is not None
            else image_modalities(chosen, catalog),
            "provider": self.config.provider.routing.model_dump(),
            "usage": {"include": True},
        }
        if image_config:
            payload["image_config"] = dict(image_config)
        return payload

    def build_decision_request(
        self,
        role: str,
        state: Sequence[str],
        questions: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Any]:
        """The body of a request to the decisions endpoint for ``role``.

        ``state`` is the texts the questions are about, and ``questions`` maps a key of the
        caller's to a question in the endpoint's own form (``decisions.py`` writes them). The
        model is resolved as any role's is. No provider routing is sent: the endpoint accepts
        the field without saying whether it honours it, so it would promise what may not hold.
        Raises ``ConfigError`` for a role that is not of kind ``decisions``.
        """
        r = self.resolve(role)
        if r.kind != "decisions":
            raise ConfigError(f"role '{role}' is of kind '{r.kind}', not 'decisions'")
        return {
            "model": r.model,
            "state": list(state),
            "questions": {key: dict(question) for key, question in questions.items()},
        }

    def complete(self, role: str, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        """POST a chat completion.

        Reference implementation; not exercised against the live API in tests.
        """
        payload = self.build_chat_request(role, messages, **kwargs)
        req = urllib.request.Request(
            self.config.provider.base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=self.headers(),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.resolve(role).timeout_s) as resp:  # noqa: S310
            return json.load(resp)


# ----------------------------------------------------------------- CLI
def _show(router: ModelRouter) -> None:
    rows = [
        (r.role, r.model, r.source, ",".join(r.requires) or "-")
        for r in router.resolve_all().values()
    ]
    widths = [
        max(len(x[i]) for x in rows + [("role", "model", "source", "requires")]) for i in range(4)
    ]
    for row in [("role", "model", "source", "requires"), *rows]:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    show = sub.add_parser("show", help="print the effective model for every role")
    show.add_argument(
        "--config",
        default=os.environ.get("LLM_CONFIG_PATH", DEFAULT_CONFIG_PATH),
    )
    args = parser.parse_args()
    if args.cmd == "show":
        _show(ModelRouter.from_file(args.config))


if __name__ == "__main__":
    main()
