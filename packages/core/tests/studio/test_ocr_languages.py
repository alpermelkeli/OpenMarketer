"""The OCR languages a reading of the copy is asked for, from the copy's letters."""

from openmarketer_core.studio.ocr_languages import OCR_LANGUAGES, languages_for


def test_english_copy_is_read_in_english_only():
    assert languages_for(["Launch day", "Try it free"]) == ("eng",)


def test_turkish_letters_add_turkish():
    assert languages_for(["Ürünü sen yaptın.", "Pazarı kim yapacak?"]) == ("eng", "tur")


def test_a_dotted_capital_i_is_turkish():
    assert languages_for(["İSTANBUL"]) == ("eng", "tur")


def test_letters_only_german_has_add_german():
    assert languages_for(["Größe zählt"]) == ("eng", "deu")


def test_a_shared_letter_is_covered_by_the_language_another_letter_chose():
    assert languages_for(["Çağ", "Öğle"]) == ("eng", "tur")


def test_letters_of_two_languages_add_both():
    assert languages_for(["Größe", "Año nuevo"]) == ("eng", "deu", "spa")


def test_letters_no_language_of_the_sandbox_has_add_nothing():
    assert languages_for(["Привет"]) == ("eng",)


def test_every_language_chosen_is_one_the_sandbox_reads():
    every = languages_for(["ğ ß ñ ã œ ì é ç ö ü"])
    assert set(every) <= set(OCR_LANGUAGES) and every[0] == "eng"
