"""Tests for reading the model provider's catalogue. HTTP goes through a fake transport."""

import httpx
import pytest

from openmarketer_core.llm_catalog import ModelCatalog, catalog_model
from openmarketer_core.llm_config import CatalogModel

BASE_URL = "https://openrouter.test/api/v1"

ENTRY = {
    "id": "anthropic/claude-sonnet-5.5",
    "name": "Claude Sonnet 5.5",
    "context_length": 200000,
    "pricing": {"prompt": "0.000003", "completion": "0.000015"},
    "supported_parameters": ["tools", "structured_outputs"],
    "architecture": {"input_modalities": ["text", "image"]},
}


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def answering(*answers):
    """A transport that gives the answers in order and records the requests it was sent."""
    requests: list[httpx.Request] = []
    pending = list(answers)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        answer = pending.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    return httpx.MockTransport(handler), requests


def catalog_with(transport, clock=None) -> ModelCatalog:
    return ModelCatalog(BASE_URL, transport=transport, clock=clock or Clock())


def listing(*entries) -> httpx.Response:
    return httpx.Response(200, json={"data": list(entries)})


def test_catalogue_is_read_from_the_providers_models_list():
    transport, requests = answering(listing(ENTRY))
    models = catalog_with(transport).models()
    assert models == {
        "anthropic/claude-sonnet-5.5": CatalogModel(
            id="anthropic/claude-sonnet-5.5",
            name="Claude Sonnet 5.5",
            context_length=200000,
            prompt_price=3.0,
            completion_price=15.0,
            supported_parameters=frozenset({"tools", "structured_outputs"}),
            input_modalities=frozenset({"text", "image"}),
        )
    }
    assert str(requests[0].url) == BASE_URL + "/models"


def test_catalogue_request_carries_no_key():
    transport, requests = answering(listing(ENTRY))
    catalog_with(transport).models()
    assert "authorization" not in requests[0].headers


def test_entry_without_a_model_slug_is_dropped():
    assert catalog_model({**ENTRY, "id": "no slug here"}) is None
    assert catalog_model({**ENTRY, "id": 42}) is None
    assert catalog_model("anthropic/claude") is None


def test_unreadable_parts_of_an_entry_are_left_out():
    model = catalog_model(
        {
            "id": "a/b",
            "name": "bad\x00name",
            "context_length": "large",
            "pricing": {"prompt": "free", "completion": -1},
            "supported_parameters": "tools",
            "architecture": None,
        }
    )
    assert model == CatalogModel(
        id="a/b",
        name="a/b",
        context_length=None,
        prompt_price=None,
        completion_price=None,
        supported_parameters=frozenset(),
        input_modalities=frozenset(),
    )


def test_catalogue_is_kept_while_it_is_fresh():
    clock = Clock()
    transport, requests = answering(listing(ENTRY), listing())
    catalog = catalog_with(transport, clock)
    catalog.models()
    clock.now = 9 * 60
    assert catalog.models() is not None and len(requests) == 1
    clock.now = 11 * 60
    assert catalog.models() == {}
    assert len(requests) == 2


def test_catalogue_that_cannot_be_read_is_none():
    transport, _ = answering(httpx.Response(503, text="down"))
    assert catalog_with(transport).models() is None


def test_answer_that_is_not_a_list_of_models_is_none():
    transport, _ = answering(httpx.Response(200, json={"data": "nothing"}))
    assert catalog_with(transport).models() is None


def test_failed_fetch_is_not_repeated_for_a_minute():
    clock = Clock()
    transport, requests = answering(httpx.ConnectError("refused"), listing(ENTRY))
    catalog = catalog_with(transport, clock)
    assert catalog.models() is None
    clock.now = 30
    assert catalog.models() is None and len(requests) == 1
    clock.now = 61
    assert catalog.models() is not None


def test_last_catalogue_is_kept_when_a_later_fetch_fails():
    clock = Clock()
    transport, _ = answering(listing(ENTRY), httpx.Response(500))
    catalog = catalog_with(transport, clock)
    first = catalog.models()
    clock.now = 11 * 60
    assert catalog.models() == first


def test_output_modalities_and_the_image_output_price_are_read():
    model = catalog_model(
        {
            "id": "google/drawer",
            "pricing": {"prompt": "0.0000005", "image_output": "0.00006"},
            "architecture": {"input_modalities": ["text"], "output_modalities": ["image", "text"]},
        }
    )
    assert model is not None
    assert model.output_modalities == frozenset({"image", "text"})
    assert model.image_output_price == pytest.approx(60.0)
