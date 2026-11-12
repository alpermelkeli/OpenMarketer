"""The font catalogue: what the sandbox image installs, what its helper reads, what models are
told, and the family a brand font is set in."""

import json
import re
from pathlib import Path

from openmarketer_core.studio import typefaces
from openmarketer_core.studio.studio_agent import rules
from openmarketer_core.studio.typefaces import (
    STYLES,
    TYPEFACES,
    Category,
    brand_typefaces_data,
    catalogue_data,
    nearest_typeface,
    typeface_named,
    typography_guide,
)

SANDBOX = Path(__file__).resolve().parents[4] / "deploy" / "sandbox" / "studio"


def _installed_packages() -> set[str]:
    dockerfile = (SANDBOX / "Dockerfile").read_text(encoding="utf-8")
    return set(re.findall(r"^\s+(fonts-[a-z0-9-]+)\s*\\?$", dockerfile, flags=re.MULTILINE))


def test_the_sandbox_image_installs_every_package_the_catalogue_names():
    assert typefaces.packages() <= _installed_packages()


def test_the_sandbox_helper_reads_the_catalogue_as_core_has_it():
    shipped = json.loads((SANDBOX / "typefaces.json").read_text(encoding="utf-8"))
    assert shipped == catalogue_data(), (
        "regenerate it: uv run python -m openmarketer_core.studio.typefaces > "
        "deploy/sandbox/studio/typefaces.json"
    )


def test_the_catalogue_has_a_family_of_every_category():
    assert {typeface.category for typeface in TYPEFACES} == set(Category)


def test_every_family_has_a_regular_face_and_names_only_known_styles():
    for typeface in TYPEFACES:
        assert "regular" in typeface.faces, typeface.name
        assert set(typeface.faces) <= set(STYLES), typeface.name


def test_every_face_is_a_font_file_under_the_system_fonts():
    for typeface in TYPEFACES:
        for path in typeface.faces.values():
            assert path.startswith("/usr/share/fonts/") and path.endswith((".ttf", ".otf"))


def test_family_names_are_unique():
    names = [typeface.name.casefold() for typeface in TYPEFACES]
    assert len(names) == len(set(names))


def test_a_family_is_found_whatever_its_case_and_spacing():
    found = typeface_named("  league   SPARTAN ")
    assert found is not None and found.name == "League Spartan"


def test_a_brand_font_the_sandbox_has_is_used_as_it_is():
    found = nearest_typeface("Inter (body)")
    assert (found.use, found.exact) == ("Inter", True)


def test_a_known_brand_font_the_sandbox_lacks_is_set_in_its_nearest_look():
    found = nearest_typeface("Montserrat (headline)")
    assert (found.use, found.exact) == ("League Spartan", False)


def test_an_unknown_brand_font_is_set_in_a_family_of_its_kind():
    assert nearest_typeface("Acme Mono").use == "JetBrains Mono"
    assert nearest_typeface("Acme Serif Display").use == "Noto Serif"
    assert nearest_typeface("Acme Grotesk").use == "Inter"


def test_every_nearest_look_is_a_family_of_the_catalogue():
    for use in typefaces._NEAREST.values():  # pyright: ignore[reportPrivateUsage]
        assert typeface_named(use) is not None, use


def test_the_models_are_told_the_catalogue_family_of_each_brand_font():
    assert brand_typefaces_data(["Poppins", "Lato (body)"]) == [
        {"brand_font": "Poppins", "use": "League Spartan", "in_the_sandbox": False},
        {"brand_font": "Lato (body)", "use": "Lato", "in_the_sandbox": True},
    ]


def test_the_guide_names_every_family_with_its_mood():
    guide = typography_guide()
    for typeface in TYPEFACES:
        assert f"- {typeface.name} ({typeface.category.value};" in guide
        assert typeface.mood in guide


def test_the_guide_sets_a_pairing_a_hierarchy_the_margin_and_the_helper():
    guide = " ".join(typography_guide().split())
    assert "Pair two faces at most" in guide
    assert "about 7 to 10% of the image's height" in guide
    assert "safe_margin_px" in guide
    assert "from studio_type import fit_text, font" in guide
    assert 'font-family: "League Spartan"' in guide


def test_the_director_and_the_studio_carry_the_guide():
    assert typography_guide() in rules.BRIEF_SYSTEM_PROMPT
    assert typography_guide() in rules.STUDIO_SYSTEM_PROMPT
