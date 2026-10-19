"""Read the text of one image several ways and print every reading, one after another.

Tesseract is made for documents: it misses large light words on a photograph, letters set over
a texture, and words beside an icon. A poster is read here as it is, in grey with its contrast
stretched, and inverted (light words on a dark ground become dark on light), each as sparse
text, so that a line any of the readings finds is found. What is printed is only text; the
caller compares it with the copy it expects.
"""

import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageOps

WORK = Path("/tmp/read_text")


def variants(source: Path) -> list[tuple[Path, str]]:
    WORK.mkdir(parents=True, exist_ok=True)
    image = Image.open(source).convert("RGB")
    grey = ImageOps.autocontrast(ImageOps.grayscale(image), cutoff=2)
    # Twice the size: tesseract reads letters best when they are 30 pixels high or more.
    grey = grey.resize((grey.width * 2, grey.height * 2), Image.LANCZOS)
    paths = []
    for name, picture in (("grey", grey), ("inverted", ImageOps.invert(grey))):
        path = WORK / f"{name}.png"
        picture.save(path)
        paths.append(path)
    return [(source, "3"), (source, "11"), (paths[0], "11"), (paths[1], "11")]


def main() -> int:
    source, languages = Path(sys.argv[1]), sys.argv[2]
    for path, layout in variants(source):
        result = subprocess.run(
            ["tesseract", str(path), "stdout", "-l", languages, "--psm", layout],
            capture_output=True,
            text=True,
            check=False,
        )
        sys.stdout.write(result.stdout + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
