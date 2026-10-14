"""The model provider's catalogue: which models it serves and what each accepts.

The dashboard's model picker lists it, and a role's choice is checked against
it (``ModelRouter.check_capabilities``). OpenRouter publishes the catalogue at
``GET <base_url>/models`` without a key, so no credential is sent.

What the provider writes is untrusted text: an entry is kept only when its id
is a model slug, its name is printable text of a bounded length, and its
numbers are numbers; anything else of it is dropped. What went wrong with a
fetch is logged, never put in what a caller is given: a catalogue that cannot
be read is ``None``, and a caller says that it is not available.

The catalogue is kept for a while (``fresh_for_seconds``) because it changes
slowly and a screen reads it on every visit; a failed fetch is not repeated for
a minute, so a provider that is down does not slow every request down.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any, TypeGuard

import httpx

from openmarketer_core.llm_config import SLUG_RE, CatalogModel

logger = logging.getLogger(__name__)

MAX_ENTRIES = 5000
MAX_TEXT_LENGTH = 200
TOKENS_PER_PRICE_UNIT = 1_000_000


class ModelCatalog:
    """The provider's catalogue, fetched when first asked for and kept while it is fresh."""

    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        fresh_for_seconds: float = 10 * 60,
        retry_after_seconds: float = 60,
        timeout_seconds: float = 10,
    ) -> None:
        self._url = base_url.rstrip("/") + "/models"
        self._transport = transport
        self._clock = clock
        self._fresh_for = fresh_for_seconds
        self._retry_after = retry_after_seconds
        self._timeout = timeout_seconds
        self._lock = threading.Lock()
        self._models: Mapping[str, CatalogModel] | None = None
        self._next_fetch_at = -math.inf

    def models(self) -> Mapping[str, CatalogModel] | None:
        """Every model of the catalogue by id, or ``None`` when it cannot be read now."""
        with self._lock:
            now = self._clock()
            if now < self._next_fetch_at:
                return self._models
            fetched = self._fetch()
            if fetched is None:
                self._next_fetch_at = now + self._retry_after
                return self._models
            self._models = fetched
            self._next_fetch_at = now + self._fresh_for
            return fetched

    def _fetch(self) -> dict[str, CatalogModel] | None:
        try:
            with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
                response = client.get(self._url, headers={"Accept": "application/json"})
        except httpx.HTTPError as e:
            logger.warning("model catalogue: request failed: %s", type(e).__name__)
            return None
        if response.status_code != 200:
            logger.warning("model catalogue: the provider answered HTTP %s", response.status_code)
            return None
        try:
            body = response.json()
        except ValueError:
            logger.warning("model catalogue: the answer is not JSON")
            return None
        entries = body.get("data") if isinstance(body, dict) else None
        if not isinstance(entries, list):
            logger.warning("model catalogue: the answer has no list of models")
            return None
        models: dict[str, CatalogModel] = {}
        for entry in entries[:MAX_ENTRIES]:
            model = catalog_model(entry)
            if model is not None:
                models[model.id] = model
        return models


def catalog_model(entry: Any) -> CatalogModel | None:
    """One entry of the catalogue as far as it can be trusted, or ``None`` without a valid id."""
    if not isinstance(entry, dict):
        return None
    model_id = entry.get("id")
    if not _is_text(model_id) or not SLUG_RE.match(model_id):
        return None
    name = entry.get("name")
    pricing = entry.get("pricing")
    if not isinstance(pricing, dict):
        pricing = {}
    architecture = entry.get("architecture")
    modalities = architecture.get("input_modalities") if isinstance(architecture, dict) else None
    return CatalogModel(
        id=model_id,
        name=name if _is_text(name) else model_id,
        context_length=_positive_int(entry.get("context_length")),
        prompt_price=_price(pricing.get("prompt")),
        completion_price=_price(pricing.get("completion")),
        supported_parameters=_words(entry.get("supported_parameters")),
        input_modalities=_words(modalities),
    )


def _is_text(value: Any) -> TypeGuard[str]:
    return isinstance(value, str) and 0 < len(value) <= MAX_TEXT_LENGTH and value.isprintable()


def _positive_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _price(value: Any) -> float | None:
    """A price per token, as text or a number, in US dollars per million tokens."""
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        return None
    try:
        per_token = float(value)
    except ValueError:
        return None
    if not math.isfinite(per_token) or per_token < 0:
        return None
    return per_token * TOKENS_PER_PRICE_UNIT


def _words(value: Any) -> frozenset[str]:
    if not isinstance(value, list):
        return frozenset()
    return frozenset(word for word in value if _is_text(word) and len(word) <= 64)
