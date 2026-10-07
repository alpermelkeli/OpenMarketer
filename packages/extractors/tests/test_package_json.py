"""Tests for the package.json extractor."""

import json

import pytest

from openmarketer_core.intake import RepoFiles
from openmarketer_extractors.package_json import PackageJsonExtractor


def extract(tmp_path, manifests: dict[str, object], **kwargs):
    for path, manifest in manifests.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        text = manifest if isinstance(manifest, str) else json.dumps(manifest, indent=2)
        target.write_text(text)
    return list(PackageJsonExtractor().extract(RepoFiles(tmp_path, **kwargs)))


def values(facts, kind):
    return [f.value for f in facts if f.kind == kind]


NEXT_APP = {
    "name": "acme-web",
    "version": "1.2.0",
    "description": "  Invoices without the busywork  ",
    "homepage": "https://acme.example",
    "keywords": ["invoices", "saas"],
    "dependencies": {"next": "16.0.0", "react": "19", "react-dom": "19", "stripe": "^17"},
    "devDependencies": {"@vercel/analytics": "^1", "typescript": "^5"},
}


def test_basic_fields_with_line_numbers(tmp_path):
    facts = extract(tmp_path, {"package.json": NEXT_APP})
    assert values(facts, "manifest.name") == ["acme-web"]
    assert values(facts, "manifest.description") == ["Invoices without the busywork"]
    assert values(facts, "manifest.keywords") == [["invoices", "saas"]]
    name = next(f for f in facts if f.kind == "manifest.name")
    assert (name.file, name.start_line, name.end_line) == ("package.json", 2, 2)


def test_web_framework_and_sdks(tmp_path):
    facts = extract(tmp_path, {"package.json": NEXT_APP})
    assert values(facts, "manifest.framework") == ["nextjs", "react"]
    assert values(facts, "manifest.platform") == ["web"]
    assert values(facts, "sdk.payments") == ["stripe"]
    assert values(facts, "sdk.analytics") == ["vercel_analytics"]
    stripe = next(f for f in facts if f.kind == "sdk.payments")
    assert stripe.start_line == 14


def test_react_native_app_targets_both_stores(tmp_path):
    manifest = {
        "name": "acme-mobile",
        "dependencies": {
            "expo": "~52",
            "react-native": "0.76",
            "react-native-purchases": "^8",
            "@react-native-firebase/analytics": "^21",
            "posthog-react-native": "^3",
        },
    }
    facts = extract(tmp_path, {"package.json": manifest})
    assert values(facts, "manifest.framework") == ["expo", "react_native"]
    assert values(facts, "manifest.platform") == ["ios", "android"]
    assert values(facts, "sdk.payments") == ["revenuecat"]
    assert values(facts, "sdk.analytics") == ["firebase", "posthog"]


def test_bin_field_means_a_command_line_tool(tmp_path):
    facts = extract(tmp_path, {"package.json": {"name": "acme-cli", "bin": {"acme": "cli.js"}}})
    assert values(facts, "manifest.platform") == ["cli"]


def test_same_sdk_is_reported_once(tmp_path):
    manifest = {"dependencies": {"stripe": "1", "@stripe/stripe-js": "1", "@stripe/react": "1"}}
    facts = extract(tmp_path, {"package.json": manifest})
    assert values(facts, "sdk.payments") == ["stripe"]


def test_every_manifest_in_a_monorepo_is_read(tmp_path):
    facts = extract(
        tmp_path,
        {
            "package.json": {"name": "root", "private": True},
            "apps/web/package.json": {"name": "web", "dependencies": {"next": "16"}},
            "node_modules/next/package.json": {"name": "next"},
        },
    )
    assert [(f.file, f.value) for f in facts if f.kind == "manifest.name"] == [
        ("package.json", "root"),
        ("apps/web/package.json", "web"),
    ]


@pytest.mark.parametrize("content", ["{ not json", "[1, 2, 3]", '"just a string"', ""])
def test_malformed_manifests_are_skipped(tmp_path, content):
    assert extract(tmp_path, {"package.json": content}) == []


def test_unexpected_value_types_are_ignored(tmp_path):
    manifest = {"name": 42, "description": None, "keywords": "x", "dependencies": ["next"]}
    assert extract(tmp_path, {"package.json": manifest}) == []


def test_long_text_is_truncated(tmp_path):
    facts = extract(tmp_path, {"package.json": {"description": "x" * 5000}})
    description = values(facts, "manifest.description")[0]
    assert isinstance(description, str) and len(description) == 500


def test_manifest_with_a_secret_finding_is_not_read(tmp_path):
    facts = extract(tmp_path, {"package.json": NEXT_APP}, secret_files={"package.json"})
    assert facts == []
