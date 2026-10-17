"""What a workspace can choose about its models from the dashboard, and what a valid choice is.

Three kinds of choice, each a layer the model router already has
(``llm_config.py``):

- the model of a tier, which every role on that tier follows;
- for one role: its model, fallbacks, temperature, length limit and reasoning
  effort, each left to the configuration file when it is not chosen;
- the request parameters a model rejects, so that the router leaves them out
  of a request to it.

A refusal is a ``ModelChoiceRefused`` that names the field and the rule and
quotes the value only when it is a number, never text. The bounds are against
typing mistakes, not a model's real limits: the provider refuses what a model
cannot do.

This module is the rule only. It reads no database and no environment; which
choice is stored, and which value wins over it, is ``model_settings.py``.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from openmarketer_core.llm_config import (
    REJECTABLE_PARAMETERS,
    SLUG_RE,
    ModelRouter,
    RoleConfig,
)

ReasoningEffort = Literal["low", "medium", "high"]
REASONING_EFFORTS: tuple[ReasoningEffort, ...] = ("low", "medium", "high")

MAX_SLUG_LENGTH = 200
MAX_FALLBACKS = 4
MIN_TEMPERATURE = 0.0
MAX_TEMPERATURE = 2.0
MIN_MAX_TOKENS = 1
MAX_MAX_TOKENS = 1_000_000


class ModelChoiceRefused(Exception):
    """A choice breaks a rule; ``field`` is the part of the choice that does."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


@dataclass(frozen=True)
class RoleChoice:
    """What a workspace chose for one role; ``None`` leaves that part to the configuration file."""

    model: str | None = None
    fallbacks: tuple[str, ...] | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    reasoning_effort: ReasoningEffort | None = None

    @property
    def is_empty(self) -> bool:
        return not self.as_override()

    def as_override(self) -> dict[str, Any]:
        """The router's override layer for the role: only what was chosen."""
        values: dict[str, Any] = {
            "model": self.model,
            "fallbacks": None if self.fallbacks is None else list(self.fallbacks),
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "reasoning_effort": self.reasoning_effort,
        }
        return {key: value for key, value in values.items() if value is not None}


@dataclass(frozen=True)
class WorkspaceModelChoices:
    """Everything a workspace chose about its models, ready to be laid over the router."""

    tiers: Mapping[str, str] = field(default_factory=dict)
    roles: Mapping[str, RoleChoice] = field(default_factory=dict)
    rejected_parameters: Mapping[str, frozenset[str]] = field(default_factory=dict)

    def router_for(self, router: ModelRouter) -> ModelRouter:
        """``router`` with these choices as its workspace layers; the environment still wins."""
        return router.with_workspace(
            overrides={role: choice.as_override() for role, choice in self.roles.items()},
            tiers=self.tiers,
            rejected_parameters=self.rejected_parameters,
        )

    def without_role(self, role: str) -> WorkspaceModelChoices:
        """These choices with nothing chosen for ``role``: what removing its choice leads to."""
        roles = {name: choice for name, choice in self.roles.items() if name != role}
        return WorkspaceModelChoices(self.tiers, roles, self.rejected_parameters)


def acceptable_model(value: Any, field: str) -> str:
    """``value`` without the whitespace around it, if it is a model slug ("provider/model")."""
    if not isinstance(value, str):
        raise ModelChoiceRefused(field, "a model is text of the form provider/model")
    slug = value.strip()
    if not slug or len(slug) > MAX_SLUG_LENGTH or not SLUG_RE.match(slug):
        raise ModelChoiceRefused(
            field,
            f"a model is of the form provider/model, at most {MAX_SLUG_LENGTH} characters"
            " of letters, digits and . _ - : + /",
        )
    return slug


def acceptable_role_choice(choice: RoleChoice, role: RoleConfig) -> RoleChoice:
    """``choice`` with its models trimmed, if every part of it is valid for ``role``.

    A role that is not a chat role (one that makes embeddings, answers decisions or makes
    images) takes a model only: the other parts are those of a chat request.
    """
    if role.kind != "chat":
        for part in ("fallbacks", "temperature", "max_tokens", "reasoning_effort"):
            if getattr(choice, part) is not None:
                raise ModelChoiceRefused(part, "this role takes a model only")
    model = None if choice.model is None else acceptable_model(choice.model, "model")
    return RoleChoice(
        model=model,
        fallbacks=_acceptable_fallbacks(choice.fallbacks),
        temperature=_acceptable_temperature(choice.temperature),
        max_tokens=_acceptable_max_tokens(choice.max_tokens),
        reasoning_effort=_acceptable_reasoning_effort(choice.reasoning_effort),
    )


def role_choice_from_stored(value: Mapping[str, Any], role: RoleConfig) -> RoleChoice:
    """A stored role choice read back under the same rules it was stored with."""
    unknown = set(value) - {"model", "fallbacks", "temperature", "max_tokens", "reasoning_effort"}
    if unknown:
        raise ModelChoiceRefused(sorted(unknown)[0], "not a part of a role's choice")
    fallbacks = value.get("fallbacks")
    if fallbacks is not None and not isinstance(fallbacks, list):
        raise ModelChoiceRefused("fallbacks", "fallbacks are a list of models")
    return acceptable_role_choice(
        RoleChoice(
            model=value.get("model"),
            fallbacks=None if fallbacks is None else tuple(fallbacks),
            temperature=value.get("temperature"),
            max_tokens=value.get("max_tokens"),
            reasoning_effort=value.get("reasoning_effort"),
        ),
        role,
    )


def acceptable_rejected_parameters(parameters: Collection[Any]) -> frozenset[str]:
    """``parameters`` if each is one a model can be marked as rejecting."""
    accepted: set[str] = set()
    for parameter in parameters:
        if parameter not in REJECTABLE_PARAMETERS:
            raise ModelChoiceRefused(
                "parameters",
                "a model can be marked as rejecting only these parameters: "
                + ", ".join(REJECTABLE_PARAMETERS),
            )
        accepted.add(parameter)
    return frozenset(accepted)


def _acceptable_fallbacks(fallbacks: tuple[Any, ...] | None) -> tuple[str, ...] | None:
    if fallbacks is None:
        return None
    if len(fallbacks) > MAX_FALLBACKS:
        raise ModelChoiceRefused("fallbacks", f"a role has at most {MAX_FALLBACKS} fallbacks")
    models = tuple(acceptable_model(model, "fallbacks") for model in fallbacks)
    if len(set(models)) != len(models):
        raise ModelChoiceRefused("fallbacks", "a fallback is listed twice")
    return models


def _acceptable_temperature(temperature: Any) -> float | None:
    if temperature is None:
        return None
    if not _is_number(temperature) or not MIN_TEMPERATURE <= temperature <= MAX_TEMPERATURE:
        raise ModelChoiceRefused(
            "temperature",
            f"the temperature is a number from {MIN_TEMPERATURE:g} to {MAX_TEMPERATURE:g}",
        )
    return float(temperature)


def _acceptable_max_tokens(max_tokens: Any) -> int | None:
    if max_tokens is None:
        return None
    if (
        not isinstance(max_tokens, int)
        or isinstance(max_tokens, bool)
        or not MIN_MAX_TOKENS <= max_tokens <= MAX_MAX_TOKENS
    ):
        raise ModelChoiceRefused(
            "max_tokens",
            f"the length limit is a whole number of tokens from {MIN_MAX_TOKENS}"
            f" to {MAX_MAX_TOKENS:,}",
        )
    return max_tokens


def _acceptable_reasoning_effort(effort: Any) -> ReasoningEffort | None:
    if effort is None:
        return None
    if effort not in REASONING_EFFORTS:
        raise ModelChoiceRefused(
            "reasoning_effort", "the reasoning effort is one of " + ", ".join(REASONING_EFFORTS)
        )
    return cast(ReasoningEffort, effort)


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)
