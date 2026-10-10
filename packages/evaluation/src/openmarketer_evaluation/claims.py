"""What in a Product Profile counts as a claim, for scoring and for the judge.

A claim is something the profile says about the product and can cite evidence
for: the product section, each feature, and each of the other sections that is
filled in. A section left at its defaults says nothing, so it is not a claim,
even when evidence is cited next to it ("no analytics found" is not scored).

This module only reads a profile. It calls no model and opens no file.
"""

from __future__ import annotations

from dataclasses import dataclass

from openmarketer_core.profile import Evidence, Evidenced, Feature, ProductProfile

PRODUCT = "product"
# The evidenced sections other than the product and the features, in the order of the schema.
SECTIONS = ("brand", "audience", "business_model", "measurement")
FEATURE_PREFIX = "feature:"

_NOT_CONTENT = {"evidence", "confidence"}


@dataclass(frozen=True)
class Claim:
    """One statement of a profile, with what it cites and how sure the profile is of it.

    ``key`` names the part of the profile: ``product``, a section name, or
    ``feature:<id>``.
    """

    key: str
    statement: str
    evidence: tuple[Evidence, ...]
    confidence: float


def feature_key(feature: Feature) -> str:
    return f"{FEATURE_PREFIX}{feature.id}"


def is_filled_in(section: Evidenced) -> bool:
    """Whether a section says anything: some field other than evidence and confidence is set."""
    said = section.model_dump(exclude=_NOT_CONTENT)
    nothing = type(section).model_construct().model_dump(exclude=_NOT_CONTENT)
    return said != nothing


def section_of(profile: ProductProfile, name: str) -> Evidenced:
    """The section called ``name`` (one of ``SECTIONS``)."""
    sections: dict[str, Evidenced] = {
        "brand": profile.brand,
        "audience": profile.audience,
        "business_model": profile.business_model,
        "measurement": profile.measurement,
    }
    return sections[name]


def _listed(values: list[str]) -> str:
    return ", ".join(values) if values else "none"


def _statement_of_section(profile: ProductProfile, name: str) -> str:
    if name == "brand":
        brand = profile.brand
        return (
            f"Brand voice: {brand.voice or 'not stated'}. "
            f"Brand colours: {_listed(brand.palette)}. "
            f"Never to be mentioned: {_listed(brand.do_not_mention)}."
        )
    if name == "audience":
        audience = profile.audience
        return (
            f"Primary audience: {audience.primary or 'not stated'}. "
            f"Their pains: {_listed(audience.pains)}."
        )
    if name == "business_model":
        business = profile.business_model
        trial = "none stated" if business.trial_days is None else f"{business.trial_days} days"
        return f"Business model: {business.type.value}. Trial: {trial}."
    measurement = profile.measurement
    deep_links = {None: "not stated", True: "yes", False: "no"}[measurement.deep_links]
    return (
        f"Analytics in use: {_listed(measurement.analytics)}. "
        f"Attribution: {measurement.attribution or 'not stated'}. "
        f"Deep links: {deep_links}."
    )


def _claim(key: str, statement: str, part: Evidenced) -> Claim:
    return Claim(
        key=key, statement=statement, evidence=tuple(part.evidence), confidence=part.confidence
    )


def claims_of(profile: ProductProfile) -> list[Claim]:
    """The claims of a profile: the product, each filled-in section, then each feature."""
    product = profile.product
    claims = [
        _claim(
            PRODUCT,
            f"The product is called {product.name!r}, is of type {product.type}, "
            f"and runs on: {_listed(product.platforms)}.",
            product,
        )
    ]
    for name in SECTIONS:
        section = section_of(profile, name)
        if is_filled_in(section):
            claims.append(_claim(name, _statement_of_section(profile, name), section))
    for feature in profile.features:
        statement = f"The product has this feature: {feature.description}"
        claims.append(_claim(feature_key(feature), statement, feature))
    return claims
