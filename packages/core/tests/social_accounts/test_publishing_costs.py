"""Tests for what publishing a post is reckoned to cost, read from configuration."""

from decimal import Decimal
from pathlib import Path

import pytest

from openmarketer_core.social_accounts.catalog import SocialPlatform
from openmarketer_core.social_accounts.publishing_costs import (
    DEFAULT_PUBLISHING_CONFIG_PATH,
    PublishingCostsInvalid,
    read_publishing_costs,
)

REPOSITORY = Path(__file__).resolve().parents[4]


def written(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "publishing.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_the_repositorys_configuration_prices_a_post_on_every_platform():
    costs = read_publishing_costs(REPOSITORY / DEFAULT_PUBLISHING_CONFIG_PATH)

    assert costs.estimate(SocialPlatform.X, with_image=False) > 0


def test_a_post_with_an_image_is_priced_apart(tmp_path):
    path = written(
        tmp_path,
        "version: 1\nplatforms:\n  x:\n    post_usd: 0.01\n    post_with_image_usd: 0.025\n",
    )

    costs = read_publishing_costs(path)

    assert costs.estimate(SocialPlatform.X, with_image=False) == Decimal("0.01")
    assert costs.estimate(SocialPlatform.X, with_image=True) == Decimal("0.025")


def test_a_configuration_without_a_platform_of_the_catalog_is_refused(tmp_path):
    path = written(tmp_path, "version: 1\nplatforms: {}\n")

    with pytest.raises(PublishingCostsInvalid, match="platforms lacks x"):
        read_publishing_costs(path)


@pytest.mark.parametrize(
    "costs",
    [
        "post_usd: -1\n    post_with_image_usd: 0.01",
        "post_usd: 50\n    post_with_image_usd: 0.01",
        "post_usd: 0.01\n    post_with_image_usd: 0.01\n    link_usd: 0.2",
    ],
)
def test_a_cost_out_of_bounds_or_a_field_it_does_not_know_is_refused(tmp_path, costs):
    path = written(tmp_path, f"version: 1\nplatforms:\n  x:\n    {costs}\n")

    with pytest.raises(PublishingCostsInvalid, match="does not fit"):
        read_publishing_costs(path)


def test_a_file_that_cannot_be_read_says_so_without_its_text(tmp_path):
    with pytest.raises(PublishingCostsInvalid, match="cannot be read: FileNotFoundError"):
        read_publishing_costs(tmp_path / "missing.yaml")
