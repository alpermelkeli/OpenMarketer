"""Specifications of what an images role's request asks back, and of the check of its models."""

from openmarketer_core.llm_config import (
    CapabilityProblem,
    CatalogModel,
    Config,
    ModelProblem,
    ModelRouter,
    RoleConfig,
    image_modalities,
)

DRAWER = RoleConfig(
    kind="images",
    model="google/draws-and-writes",
    fallbacks=["black-forest-labs/draws-only"],
    requires=["image_output", "vision"],
)


def entry(slug: str, *, inputs: set[str], outputs: set[str]) -> CatalogModel:
    return CatalogModel(
        id=slug,
        name=slug,
        context_length=None,
        prompt_price=None,
        completion_price=None,
        supported_parameters=frozenset(),
        input_modalities=frozenset(inputs),
        output_modalities=frozenset(outputs),
    )


GEMINI_LIKE = entry("google/draws-and-writes", inputs={"text", "image"}, outputs={"image", "text"})
FLUX_LIKE = entry("black-forest-labs/draws-only", inputs={"text", "image"}, outputs={"image"})
RECRAFT_FLASH_LIKE = entry("recraft/reads-text-only", inputs={"text"}, outputs={"image"})


def router(role: RoleConfig = DRAWER) -> ModelRouter:
    config = Config(
        tiers={"fast": "a/fast", "balanced": "a/balanced", "strong": "a/strong"},
        defaults={"model": "a/default"},  # type: ignore[arg-type]
        roles={"drawer": role},
    )
    return ModelRouter(config, env={})


def catalogue(*models: CatalogModel) -> dict[str, CatalogModel]:
    return {model.id: model for model in models}


def test_models_that_also_write_text_are_asked_for_an_image_and_text():
    assert image_modalities(["google/draws-and-writes"], catalogue(GEMINI_LIKE)) == [
        "image",
        "text",
    ]


def test_a_model_that_makes_images_only_is_asked_for_an_image_only():
    models = ["black-forest-labs/draws-only"]
    assert image_modalities(models, catalogue(FLUX_LIKE)) == ["image"]


def test_one_image_only_fallback_makes_the_whole_request_ask_for_an_image_only():
    request = router().build_image_request("drawer", [], catalog=catalogue(GEMINI_LIKE, FLUX_LIKE))
    assert request["models"] == ["google/draws-and-writes", "black-forest-labs/draws-only"]
    assert request["modalities"] == ["image"]


def test_without_a_catalogue_the_request_asks_for_an_image_and_text():
    assert router().build_image_request("drawer", [])["modalities"] == ["image", "text"]


def test_a_model_the_catalogue_does_not_list_leaves_the_request_as_it_was():
    assert image_modalities(["vendor/unknown"], catalogue(GEMINI_LIKE)) == ["image", "text"]


def test_a_model_whose_outputs_the_catalogue_does_not_give_is_not_taken_for_image_only():
    silent = entry("vendor/silent", inputs={"text"}, outputs=set())
    assert image_modalities(["vendor/silent"], catalogue(silent)) == ["image", "text"]


def test_an_image_model_that_reads_no_images_cannot_serve_a_role_that_edits():
    role = RoleConfig(
        kind="images", model="recraft/reads-text-only", requires=["image_output", "vision"]
    )
    chosen = router(role)
    problems = chosen.check_capabilities(chosen.resolve("drawer"), catalogue(RECRAFT_FLASH_LIKE))
    assert problems == [ModelProblem("recraft/reads-text-only", CapabilityProblem.NO_IMAGE_INPUT)]


def test_an_image_only_model_that_reads_images_serves_an_image_role():
    role = RoleConfig(
        kind="images", model="black-forest-labs/draws-only", requires=["image_output", "vision"]
    )
    chosen = router(role)
    assert chosen.check_capabilities(chosen.resolve("drawer"), catalogue(FLUX_LIKE)) == []
