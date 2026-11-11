"""Tests for the catalog of platforms an account can be connected on."""

import ast
from pathlib import Path

import pytest

from openmarketer_core import social_accounts
from openmarketer_core.credentials.catalog import CREDENTIALS
from openmarketer_core.social_accounts.catalog import (
    PLATFORMS,
    SocialPlatform,
    UnknownPlatform,
    X,
    connectable_platform,
)
from openmarketer_core.social_accounts.x import __file__ as x_package_file


def test_x_is_the_one_platform_and_needs_the_x_app_s_key_and_secret():
    assert PLATFORMS == (X,)
    assert (X.platform, X.app_key.name, X.app_secret.name) == (
        SocialPlatform.X,
        "x_api_key",
        "x_api_secret",
    )


def test_every_platform_has_a_catalog_entry_and_its_app_is_in_the_credential_catalog():
    assert {entry.platform for entry in PLATFORMS} == set(SocialPlatform)
    for entry in PLATFORMS:
        assert entry.app_key in CREDENTIALS
        assert entry.app_secret in CREDENTIALS


def test_platform_is_found_by_its_slug():
    assert connectable_platform("x") is X


@pytest.mark.parametrize("slug", ["", "X", "twitter", "linkedin", "secret-token"])
def test_slug_that_is_not_in_the_catalog_is_refused_without_repeating_it(slug):
    with pytest.raises(UnknownPlatform) as refused:
        connectable_platform(slug)
    assert str(refused.value) == "there is no such platform to connect"


@pytest.mark.parametrize("package_file", [social_accounts.__file__, x_package_file])
def test_feature_packages_import_nothing(package_file):
    tree = ast.parse(Path(str(package_file)).read_text())
    assert not [node for node in tree.body if isinstance(node, ast.Import | ast.ImportFrom)]
