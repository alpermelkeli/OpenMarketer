"""Product Profile schema.

The Product Profile is the structured description of a product that the
repository analyzer produces and every later step reads. It is defined once
here; the JSON Schema used to validate model output is exported from these
models with ``ProductProfile.model_json_schema()``.

Every section carries the evidence it was derived from (file and line range in
the analysed repository) and a confidence score, so each claim can be traced
and reviewed. Only features marked ``live`` may appear in public content.

Review state and version numbers are not part of this schema: they belong to
the stored profile record, not to the content a model is allowed to write.
"""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = 1

LINES_RE = re.compile(r"^(\d+)(?:-(\d+))?$")

# Open vocabularies: a product can be anything, and playbooks or extractors may
# introduce values the core has never heard of. The enums below list the
# well-known values, they do not limit the field.
Slug = Annotated[str, Field(pattern=r"^[a-z0-9]+(_[a-z0-9]+)*$", max_length=64)]
FeatureId = Annotated[str, Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$", max_length=64)]
LanguageTag = Annotated[str, Field(pattern=r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$")]
HexColor = Annotated[str, Field(pattern=r"^#[0-9A-Fa-f]{6}$")]
Confidence = Annotated[float, Field(ge=0.0, le=1.0)]


class ProductType(StrEnum):
    """Well-known product types, each with a playbook in the core distribution."""

    CONSUMER_APP = "consumer_app"
    B2B_SAAS = "b2b_saas"
    DEV_TOOL = "dev_tool"
    GAME = "game"
    ECOMMERCE = "ecommerce"


class Platform(StrEnum):
    """Well-known platforms. Others (a watch, a browser extension, an API) are just slugs."""

    IOS = "ios"
    ANDROID = "android"
    WEB = "web"
    DESKTOP = "desktop"
    CLI = "cli"


class FeatureStatus(StrEnum):
    """The publishable flag: only ``live`` features may be mentioned publicly."""

    LIVE = "live"
    UNRELEASED = "unreleased"
    UNKNOWN = "unknown"


class BusinessModelType(StrEnum):
    FREE = "free"
    SUBSCRIPTION = "subscription"
    ONE_TIME_PURCHASE = "one_time_purchase"
    ADS = "ads"
    UNKNOWN = "unknown"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------- evidence
class Evidence(_Model):
    """A location in the analysed repository that supports a claim."""

    file: str
    lines: str | None = None  # "12" or "12-88", 1-based and inclusive

    @field_validator("file")
    @classmethod
    def _relative_path(cls, v: str) -> str:
        path = PurePosixPath(v)
        if not v.strip() or path.is_absolute() or ".." in path.parts:
            raise ValueError("must be a path relative to the repository root")
        return v

    @field_validator("lines")
    @classmethod
    def _line_range(cls, v: str | None) -> str | None:
        if v is None:
            return v
        m = LINES_RE.match(v)
        if not m:
            raise ValueError("must look like '12' or '12-88'")
        start, end = int(m.group(1)), int(m.group(2) or m.group(1))
        if start < 1 or end < start:
            raise ValueError("line range must be 1-based and ascending")
        return v


class Evidenced(_Model):
    """Base for every part of the profile that makes claims about the product."""

    evidence: list[Evidence] = Field(default_factory=list)
    confidence: Confidence = 0.0


# --------------------------------------------------------------- sections
class Product(Evidenced):
    name: str = Field(min_length=1)
    type: Slug
    platforms: list[Slug] = Field(default_factory=list)
    languages: list[LanguageTag] = Field(default_factory=list)


class Feature(Evidenced):
    id: FeatureId
    description: str = Field(min_length=1)
    status: FeatureStatus = FeatureStatus.UNKNOWN

    @property
    def publishable(self) -> bool:
        return self.status is FeatureStatus.LIVE


class Brand(Evidenced):
    voice: str | None = None
    palette: list[HexColor] = Field(default_factory=list)
    do_not_mention: list[str] = Field(default_factory=list)

    @field_validator("palette")
    @classmethod
    def _upper_case(cls, v: list[str]) -> list[str]:
        return [c.upper() for c in v]


class Audience(Evidenced):
    primary: str | None = None
    pains: list[str] = Field(default_factory=list)


class BusinessModel(Evidenced):
    type: BusinessModelType = BusinessModelType.UNKNOWN
    trial_days: int | None = Field(default=None, ge=0)


class Measurement(Evidenced):
    analytics: list[str] = Field(default_factory=list)
    attribution: str | None = None
    deep_links: bool | None = None


# ---------------------------------------------------------------- profile
class ProductProfile(_Model):
    schema_version: Literal[1] = SCHEMA_VERSION
    product: Product
    features: list[Feature] = Field(default_factory=list)
    brand: Brand = Field(default_factory=Brand)
    audience: Audience = Field(default_factory=Audience)
    business_model: BusinessModel = Field(default_factory=BusinessModel)
    measurement: Measurement = Field(default_factory=Measurement)

    @model_validator(mode="after")
    def _unique_feature_ids(self) -> ProductProfile:
        seen: set[str] = set()
        for feature in self.features:
            if feature.id in seen:
                raise ValueError(f"duplicate feature id '{feature.id}'")
            seen.add(feature.id)
        return self

    def publishable_features(self) -> list[Feature]:
        """Features that public content is allowed to mention."""
        return [f for f in self.features if f.publishable]
