"""Tests for the Product Profile schema."""

import pytest
import yaml
from openmarketer_core.profile import Evidence, FeatureStatus, ProductProfile
from pydantic import ValidationError

# The excerpt from section 6.3 of the design report.
REPORT_EXAMPLE = """
product:
  name: "Example App"
  type: consumer_app
  platforms: [ios, android]
  languages: [en, tr]
features:
  - id: offline-mode
    description: "Works without a connection"
    status: live
    evidence: [{file: "lib/sync/offline.dart", lines: "12-88"}]
    confidence: 0.86
brand:
  voice: "friendly, concise, no hype"
  palette: ["#1B6EF3", "#ffffff"]
  do_not_mention: ["pricing experiment", "internal codename"]
audience: {primary: "busy commuters", pains: ["no signal underground"]}
business_model: {type: subscription, trial_days: 7}
measurement: {analytics: [firebase], attribution: none, deep_links: true}
"""


def example() -> dict:
    return yaml.safe_load(REPORT_EXAMPLE)


def test_report_example_validates():
    p = ProductProfile.model_validate(example())
    assert p.schema_version == 1
    assert p.product.name == "Example App"
    assert p.features[0].evidence[0].lines == "12-88"
    assert p.brand.palette == ["#1B6EF3", "#FFFFFF"]
    assert p.business_model.trial_days == 7


def test_minimal_profile_gets_safe_defaults():
    p = ProductProfile.model_validate({"product": {"name": "X", "type": "dev_tool"}})
    assert p.features == []
    assert p.product.confidence == 0.0
    assert p.business_model.type == "unknown"


def test_round_trips_through_json():
    p = ProductProfile.model_validate(example())
    assert ProductProfile.model_validate_json(p.model_dump_json()) == p


def test_only_live_features_are_publishable():
    data = example()
    data["features"] += [
        {"id": "ai-coach", "description": "Behind a flag", "status": "unreleased"},
        {"id": "widgets", "description": "Status not determined"},
    ]
    p = ProductProfile.model_validate(data)
    assert p.features[2].status is FeatureStatus.UNKNOWN
    assert [f.id for f in p.publishable_features()] == ["offline-mode"]


def test_duplicate_feature_ids_are_rejected():
    data = example()
    data["features"].append(dict(data["features"][0]))
    with pytest.raises(ValidationError, match="duplicate feature id"):
        ProductProfile.model_validate(data)


def test_unknown_fields_are_rejected():
    data = example()
    data["product"]["approved"] = True
    with pytest.raises(ValidationError):
        ProductProfile.model_validate(data)


@pytest.mark.parametrize("confidence", [-0.1, 1.01])
def test_confidence_must_be_between_zero_and_one(confidence):
    data = example()
    data["features"][0]["confidence"] = confidence
    with pytest.raises(ValidationError):
        ProductProfile.model_validate(data)


@pytest.mark.parametrize("lines", ["12", "12-88", None])
def test_valid_evidence_lines(lines):
    assert Evidence(file="src/app.ts", lines=lines).lines == lines


@pytest.mark.parametrize("lines", ["0", "88-12", "12-", "a-b", "12:88"])
def test_invalid_evidence_lines(lines):
    with pytest.raises(ValidationError):
        Evidence(file="src/app.ts", lines=lines)


@pytest.mark.parametrize("file", ["", "/etc/passwd", "../outside.txt", "src/../../x"])
def test_evidence_file_must_stay_inside_the_repository(file):
    with pytest.raises(ValidationError):
        Evidence(file=file)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("product", "type", "social_network"),
        ("product", "platforms", ["symbian"]),
        ("product", "languages", ["English"]),
        ("brand", "palette", ["blue"]),
        ("business_model", "trial_days", -1),
    ],
)
def test_invalid_values_are_rejected(section, field, value):
    data = example()
    data[section][field] = value
    with pytest.raises(ValidationError):
        ProductProfile.model_validate(data)


def test_feature_id_must_be_a_slug():
    data = example()
    data["features"][0]["id"] = "Offline Mode"
    with pytest.raises(ValidationError):
        ProductProfile.model_validate(data)


def test_json_schema_export():
    schema = ProductProfile.model_json_schema()
    assert schema["required"] == ["product"]
    assert schema["additionalProperties"] is False
    assert {"Feature", "Evidence", "Brand"} <= set(schema["$defs"])
