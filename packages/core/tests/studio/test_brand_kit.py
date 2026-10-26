"""Tests for the brand kit a studio run takes from the approved Product Profile."""

from openmarketer_core.profile import ProductProfile
from openmarketer_core.studio.brand_kit import EMPTY_BRAND_KIT, brand_kit_of


def a_profile() -> ProductProfile:
    return ProductProfile.model_validate(
        {
            "product": {"name": "Acme", "type": "web_app", "platforms": ["web", "ios"]},
            "features": [
                {"id": "sync", "description": "Syncs notes.", "status": "live"},
                {"id": "ai", "description": "Writes notes.", "status": "unreleased"},
            ],
            "brand": {
                "voice": "plain",
                "palette": ["#112233"],
                "typography": [{"family": "Inter", "role": "body"}, {"family": "Lora"}],
                "do_not_mention": ["Rival"],
            },
            "audience": {"primary": "students"},
        }
    )


def test_the_kit_takes_the_brand_and_only_live_features_from_the_profile():
    kit = brand_kit_of(a_profile())
    assert kit.name == "Acme"
    assert kit.kind == "web app on web, ios"
    assert kit.voice == "plain"
    assert kit.palette == ("#112233",)
    assert kit.fonts == ("Inter (body)", "Lora")
    assert kit.do_not_mention == ("Rival",)
    assert kit.audience == "students"
    assert kit.features == ("Syncs notes.",)


def test_no_approved_profile_is_the_empty_kit():
    assert brand_kit_of(None) == EMPTY_BRAND_KIT
    assert EMPTY_BRAND_KIT.as_data()["logo"] is None


def test_the_studio_model_is_shown_the_fonts():
    assert brand_kit_of(a_profile()).as_data()["fonts"] == ["Inter (body)", "Lora"]
