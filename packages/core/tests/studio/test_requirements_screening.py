"""Specifications of the reading of the extra packages a sandbox run asks for."""

from openmarketer_core.studio.code_screening import (
    MAX_REQUIREMENTS,
    Requirement,
    screen_requirements,
)


def test_a_bare_name_is_accepted() -> None:
    assert screen_requirements(["scikit-image"]).requirements == (Requirement("scikit-image"),)


def test_an_exact_version_is_accepted() -> None:
    verdict = screen_requirements(["svgwrite==1.4.3"])
    assert verdict.requirements == (Requirement("svgwrite", "1.4.3"),)


def test_a_name_is_normalized() -> None:
    assert screen_requirements(["Scikit_Image"]).requirements == (Requirement("scikit-image"),)


def test_a_url_is_refused() -> None:
    assert not screen_requirements(["https://example.com/evil.whl"]).accepted


def test_a_direct_reference_is_refused() -> None:
    assert not screen_requirements(["pkg @ https://example.com/pkg.whl"]).accepted


def test_an_option_is_refused() -> None:
    assert not screen_requirements(["--index-url=https://example.com"]).accepted


def test_a_path_is_refused() -> None:
    assert not screen_requirements(["./local_package"]).accepted


def test_a_version_range_is_refused() -> None:
    assert not screen_requirements(["svgwrite>=1.0"]).accepted


def test_an_extra_is_refused() -> None:
    assert not screen_requirements(["svgwrite[all]"]).accepted


def test_an_environment_marker_is_refused() -> None:
    assert not screen_requirements(['svgwrite; python_version > "3"']).accepted


def test_a_denied_package_is_refused() -> None:
    assert screen_requirements(["requests"]).reasons == (
        "package 1 (requests) may not be installed",
    )


def test_a_preinstalled_library_is_refused() -> None:
    assert not screen_requirements(["Pillow==9.0.0"]).accepted


def test_pip_itself_is_refused() -> None:
    assert not screen_requirements(["pip"]).accepted


def test_a_package_asked_twice_is_refused() -> None:
    assert screen_requirements(["svgwrite", "SVGWrite"]).reasons == (
        "package 2 (svgwrite) is asked twice",
    )


def test_more_than_the_maximum_is_refused() -> None:
    names = [f"package{i}" for i in range(MAX_REQUIREMENTS + 1)]
    assert not screen_requirements(names).accepted


def test_the_pip_argument_pins_the_version() -> None:
    assert Requirement("svgwrite", "1.4.3").pip_argument() == "svgwrite==1.4.3"
