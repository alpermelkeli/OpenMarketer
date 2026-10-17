"""Decision access for agents: yes-or-no questions about a list of texts, one call per batch.

Agents depend on the small ``DecisionModel`` protocol, not on a provider. The default
implementation resolves a role of kind ``decisions`` with ``ModelRouter`` and posts to
OpenRouter's decisions endpoint (``provider.decisions_url``, in alpha), which answers each
question with a probability and writes no text. That is the point of it: a model that reads
untrusted text and can only answer numbers cannot be talked into a tool call or a message.

Only one kind of question is asked, the yes-or-no one (the endpoint's ``noul``); the endpoint
knows others (choice, score, box), added here when something needs them. No routing
preferences are sent (``ModelRouter.build_decision_request`` says why).

The answer is read strictly: every question asked is answered with a probability, and nothing
else is taken. What the endpoint writes when it refuses a request is not repeated in the
error, as in ``llm.py``: its message echoes the request. The error says the HTTP status and
the response goes to the log.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from openmarketer_core.llm import LLMError, model_name_to_keep, reported_status
from openmarketer_core.llm_config import ModelRouter

logger = logging.getLogger(__name__)

YES_OR_NO = "noul"


class DecisionError(LLMError):
    """The decision call failed or answered something unusable.

    ``status`` and ``may_pass`` mean what they mean for a chat call (``LLMError``).
    """


@dataclass(frozen=True)
class YesNoQuestion:
    """One question about the state, and what each answer means to whoever asks."""

    instructions: str
    true_means: str
    false_means: str

    def as_request(self) -> dict[str, Any]:
        """The question in the endpoint's own form."""
        return {
            "type": YES_OR_NO,
            "instructions": self.instructions,
            "criteria": {"true": self.true_means, "false": self.false_means},
        }


@dataclass(frozen=True)
class DecisionReply:
    """The probability that the answer is yes, by the key of each question asked."""

    answers: Mapping[str, float]
    model: str
    cost_usd: float


class DecisionModel(Protocol):
    def decide(
        self, role: str, state: Sequence[str], questions: Mapping[str, YesNoQuestion]
    ) -> DecisionReply: ...


@dataclass
class RouterDecisionModel:
    """``DecisionModel`` backed by the per-role model configuration.

    ``api_key`` is the provider's key for the calls of this model, for a caller that has
    resolved one itself; without it the key is the one in the router's environment. It is
    left out of ``repr``, which ends up in logs and tracebacks.
    """

    router: ModelRouter
    transport: httpx.BaseTransport | None = None
    calls: list[DecisionReply] = field(default_factory=list)
    api_key: str | None = field(default=None, repr=False)

    def decide(
        self, role: str, state: Sequence[str], questions: Mapping[str, YesNoQuestion]
    ) -> DecisionReply:
        payload = self.router.build_decision_request(
            role, state, {key: question.as_request() for key, question in questions.items()}
        )
        body = self._post(role, payload)
        reply = DecisionReply(
            answers=_answers(body.get("answers"), asked=questions.keys()),
            model=model_name_to_keep(body.get("model"), asked_for=payload["model"]),
            cost_usd=_cost(body.get("usage")),
        )
        self.calls.append(reply)
        return reply

    def _post(self, role: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            with httpx.Client(
                timeout=self.router.resolve(role).timeout_s, transport=self.transport
            ) as client:
                response = client.post(
                    self.router.config.provider.decisions_url,
                    json=payload,
                    headers=self.router.headers(self.api_key),
                )
        except httpx.HTTPError as e:
            raise DecisionError(f"request failed: {type(e).__name__}") from e
        if response.status_code != 200:
            logger.warning(
                "decisions endpoint answered HTTP %s: %r",
                response.status_code,
                response.text[:300],
            )
            raise DecisionError(
                f"the decisions endpoint answered HTTP {response.status_code}",
                status=response.status_code,
            )
        try:
            body = response.json()
        except ValueError:
            logger.warning("decisions endpoint answered no JSON: %r", response.text[:300])
            raise DecisionError(
                "the decisions endpoint answered something that is not JSON"
            ) from None
        if not isinstance(body, dict):
            raise DecisionError("the decisions endpoint answered something that is not an object")
        if "error" in body:
            error = body["error"]
            logger.warning("decisions endpoint reported an error: %r", str(error)[:300])
            raise DecisionError(
                "the decisions endpoint reported an error", status=reported_status(error)
            )
        return body


def _answers(answers: Any, *, asked: Collection[str]) -> dict[str, float]:
    """The probability of each question asked, or ``DecisionError`` naming the keys at fault."""
    if not isinstance(answers, dict):
        raise DecisionError("the decisions endpoint answered no answers")
    asked_keys = set(asked)
    # The keys of questions not asked are the endpoint's text: counted, never repeated.
    unasked = len(set(answers) - asked_keys)
    if unasked:
        raise DecisionError(f"the decisions endpoint answered {unasked} questions not asked")
    missing = sorted(asked_keys - set(answers))
    if missing:
        raise DecisionError(f"the decisions endpoint left questions unanswered: {missing}")
    probabilities: dict[str, float] = {}
    unreadable: list[str] = []
    for key in sorted(asked_keys):
        probability = _probability(answers[key])
        if probability is None:
            unreadable.append(key)
        else:
            probabilities[key] = probability
    if unreadable:
        raise DecisionError(
            f"the decisions endpoint answered no probability from 0 to 1 for: {unreadable}"
        )
    return probabilities


def _probability(answer: Any) -> float | None:
    if not isinstance(answer, dict) or answer.get("type") != YES_OR_NO:
        return None
    value = answer.get(YES_OR_NO)
    if not isinstance(value, int | float) or isinstance(value, bool):
        return None
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        return None
    return float(value)


def _cost(usage: Any) -> float:
    """What the call cost as the endpoint reported it; nothing reported counts as nothing."""
    cost = usage.get("cost") if isinstance(usage, dict) else None
    if isinstance(cost, int | float) and not isinstance(cost, bool) and math.isfinite(cost):
        return max(float(cost), 0.0)
    return 0.0
