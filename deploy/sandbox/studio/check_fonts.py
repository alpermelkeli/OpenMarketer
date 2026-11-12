"""Check the font catalogue against the image: every face is there and renders Turkish.

    check_fonts.py /opt/studio/lib/typefaces.json

Run when the image is built (a failure stops the build) and by the container tests. For every
face of the catalogue: its file exists, its character map holds every letter of ``LETTERS``,
and fontconfig, which Chromium asks, knows its family by the catalogue's name. Prints one line
per problem and exits 1 when there is any.
"""

import json
import subprocess
import sys
from pathlib import Path

from fontTools.ttLib import TTFont

# The Turkish letters (core's typefaces.TURKISH_LETTERS) and the other Latin letters the
# studio's copy most often has.
LETTERS = "ğĞşŞıİçÇöÖüÜäßéèêàâáíóúñãõ"


def problems_of(catalogue: dict) -> list[str]:
    problems = []
    for typeface in catalogue["typefaces"]:
        name = typeface["name"]
        for style, path in typeface["faces"].items():
            if not Path(path).is_file():
                problems.append(f"{name} {style}: {path} is missing")
                continue
            with TTFont(path, fontNumber=0, lazy=True) as face:
                mapped = face.getBestCmap() or {}
            missing = [letter for letter in LETTERS if ord(letter) not in mapped]
            if missing:
                problems.append(f"{name} {style}: lacks {''.join(missing)}")
        matched = subprocess.run(
            ["fc-match", "--format=%{family}", name], capture_output=True, text=True, check=False
        ).stdout
        if name not in matched.split(","):
            problems.append(f"{name}: fontconfig matches {matched!r}")
    return problems


def main() -> int:
    catalogue = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    problems = problems_of(catalogue)
    for problem in problems:
        print(problem)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
