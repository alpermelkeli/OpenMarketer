"""What publishing a post on each platform is reckoned to cost, from ``config/publishing.yaml``.

A platform may bill its API per request to the installation's developer account (X does, and
changed its prices this year), so what a post costs is configuration and not code: a person
reads the estimate on the proposal before approving it, and the content item records it once
the post is published. It is an estimate, as the platform bills, and nothing holds a post to
it: there is no cap and no ledger.

The file names every platform of the catalog (``catalog.PLATFORMS``), with what a post costs
and what a post with an image costs, which a platform may price differently. A post with a
link is not priced: the writer's ``no_links`` rule keeps links out of every post. A file that
does not fit is ``PublishingCostsInvalid``, naming the problem, at the start of whoever reads
it.

This module reads the file it is given. It does not read the environment (the caller chooses
the path, ``PUBLISHING_CONFIG_PATH`` or ``DEFAULT_PUBLISHING_CONFIG_PATH``) and it does not
publish.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from openmarketer_core.social_accounts.catalog import PLATFORMS, SocialPlatform

DEFAULT_PUBLISHING_CONFIG_PATH = "config/publishing.yaml"  # relative to the repository root
# Far above any price a platform asks for one post: a value above it is a typo.
MAX_POST_COST_USD = Decimal(10)

PostCost = Annotated[Decimal, Field(ge=0, le=MAX_POST_COST_USD, decimal_places=6)]


class PublishingCostsInvalid(Exception):
    """The publishing configuration does not fit; the message names the problem."""


class PlatformPostCosts(BaseModel):
    """What one post on a platform is reckoned to cost, in US dollars."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    post_usd: PostCost
    post_with_image_usd: PostCost


class PublishingCosts(BaseModel):
    """What a post is reckoned to cost on every platform of the catalog."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1]
    platforms: dict[SocialPlatform, PlatformPostCosts]

    @model_validator(mode="after")
    def _every_platform(self) -> PublishingCosts:
        missing = [p.platform.value for p in PLATFORMS if p.platform not in self.platforms]
        if missing:
            raise ValueError(f"platforms lacks {', '.join(missing)}")
        return self

    def estimate(self, platform: SocialPlatform, *, with_image: bool) -> Decimal:
        """What one post on ``platform`` is reckoned to cost, with an image or without."""
        costs = self.platforms[platform]
        return costs.post_with_image_usd if with_image else costs.post_usd


def read_publishing_costs(path: str | Path) -> PublishingCosts:
    """The costs in the YAML file at ``path``; ``PublishingCostsInvalid`` when it does not fit."""
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except OSError as e:
        raise PublishingCostsInvalid(f"{path} cannot be read: {type(e).__name__}") from None
    except yaml.YAMLError:
        raise PublishingCostsInvalid(f"{path} is not YAML") from None
    try:
        return PublishingCosts.model_validate(raw)
    except ValidationError as e:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'file'}: {error['msg']}"
            for error in e.errors()
        )
        raise PublishingCostsInvalid(f"{path} does not fit: {problems}") from None
