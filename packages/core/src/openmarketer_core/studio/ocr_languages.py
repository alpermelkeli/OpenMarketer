"""Which of the sandbox's OCR languages a reading of the copy needs, decided from its letters.

Every language tesseract is given is another model it runs, so a reading names only those the
copy's letters call for, and always English (``eng``). Any language whose alphabet has a letter
reads it, so the fewest languages that between them have every letter of the copy beyond
English's are chosen, the one with the most of the letters still uncovered first: a letter only
one language has (ğ, ş, ı, İ: Turkish; ß, ä: German; ñ: Spanish; ã, õ: Portuguese) settles it,
and a shared one (ç, ö, ü) is then most often covered already. A tie goes to the language
earlier in ``OCR_LANGUAGES``. A letter of no language the sandbox reads adds nothing.

``OCR_LANGUAGES`` is the fixed set the sandbox's image installs (deploy/sandbox/studio/Dockerfile)
and its entry script accepts; a language list crosses into a container only as members of it.
It is data and pure functions, with no knowledge of the sandbox's runs.
"""

from collections.abc import Mapping, Sequence
from typing import Literal

OcrLanguage = Literal["eng", "tur", "deu", "fra", "spa", "ita", "por"]
OCR_LANGUAGES: tuple[OcrLanguage, ...] = ("eng", "tur", "deu", "fra", "spa", "ita", "por")

# The languages of the sandbox whose alphabet has each letter beyond English's, in lower case.
_LANGUAGES_OF: Mapping[str, tuple[OcrLanguage, ...]] = {
    "ğ": ("tur",),
    "ş": ("tur",),
    "ı": ("tur",),
    "ß": ("deu",),
    "ä": ("deu",),
    "ñ": ("spa",),
    "¡": ("spa",),
    "¿": ("spa",),
    "ã": ("por",),
    "õ": ("por",),
    "œ": ("fra",),
    "æ": ("fra",),
    "ë": ("fra",),
    "ï": ("fra",),
    "ÿ": ("fra",),
    "î": ("fra",),
    "û": ("fra",),
    "ì": ("ita",),
    "ò": ("ita",),
    "ö": ("deu", "tur"),
    "ü": ("deu", "tur", "fra"),
    "ç": ("tur", "fra", "por"),
    "è": ("fra", "ita"),
    "ù": ("fra", "ita"),
    "à": ("fra", "ita", "por"),
    "â": ("fra", "por", "tur"),
    "ê": ("fra", "por"),
    "ô": ("fra", "por"),
    "é": ("fra", "spa", "ita", "por"),
    "á": ("spa", "por"),
    "í": ("spa", "por", "ita"),
    "ó": ("spa", "por", "ita"),
    "ú": ("spa", "por", "ita"),
}


def languages_for(exact_copy: Sequence[str]) -> tuple[OcrLanguage, ...]:
    """The OCR languages a reading of ``exact_copy`` needs, ``eng`` first, in the order of
    ``OCR_LANGUAGES``."""
    uncovered = {
        _lower(char) for line in exact_copy for char in line if _lower(char) in _LANGUAGES_OF
    }
    chosen: set[OcrLanguage] = {"eng"}
    while uncovered:
        best = max(
            OCR_LANGUAGES[1:],
            key=lambda language: (
                sum(language in _LANGUAGES_OF[letter] for letter in uncovered),
                -OCR_LANGUAGES.index(language),
            ),
        )
        chosen.add(best)
        uncovered = {letter for letter in uncovered if best not in _LANGUAGES_OF[letter]}
    return tuple(language for language in OCR_LANGUAGES if language in chosen)


def _lower(char: str) -> str:
    # "İ".lower() is two characters (i and a combining dot); either Turkish i settles Turkish.
    return "ı" if char == "İ" else char.lower()
