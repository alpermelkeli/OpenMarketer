"""Read the text of one image two ways at once and print both readings, one after the other.

    read_text.py IMAGE LANGUAGES      LANGUAGES as tesseract takes them: "eng+tur"

Tesseract is made for documents: it misses large light words on a photograph, letters set over
a texture, and words beside an icon. A poster is read here twice, each time as sparse text
(page segmentation mode 11, which looks for words anywhere instead of for a page of lines): as
it is, and as its grey image with the contrast stretched and inverted, so that light words on a
dark ground become dark words on a light one. A line either reading finds is found. What is
printed is only text; the caller compares it with the copy it expects.

Why only this much, and why so: OCR has the sandbox's CPU quota and its time limit, and every
pass, every language and every pixel costs time. Two passes find what four did on real studio
images (a plain grey pass and a whole-page pass added nothing the others missed). The image is
enlarged only when it is small, to a shorter side of ``MIN_SHORTER_SIDE`` pixels: tesseract
reads letters best when they are 30 pixels high or more, a marketing image's words are larger
than that, and doubling a large image quadrupled the time for nothing. The caller names only
the languages the copy needs, since each one is another model tesseract runs. The two passes
run as two processes at once, each held to one thread: tesseract's own threads fight over a
container's CPU quota and made a reading slower, not faster.
"""

import os
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageOps

WORK = Path("/tmp/read_text")
MIN_SHORTER_SIDE = 1000
SPARSE_TEXT = "11"


def variants(source: Path) -> list[Path]:
    """The image as it is, enlarged when small, and its grey image inverted."""
    WORK.mkdir(parents=True, exist_ok=True)
    image = Image.open(source).convert("RGB")
    scale = MIN_SHORTER_SIDE / min(image.size)
    if scale > 1:
        image = image.resize(
            (round(image.width * scale), round(image.height * scale)), Image.LANCZOS
        )
    grey = ImageOps.autocontrast(ImageOps.grayscale(image), cutoff=2)
    paths = []
    for name, picture in (("as_is", image), ("inverted", ImageOps.invert(grey))):
        path = WORK / f"{name}.png"
        picture.save(path)
        paths.append(path)
    return paths


def main() -> int:
    source, languages = Path(sys.argv[1]), sys.argv[2]
    environment = {**os.environ, "OMP_THREAD_LIMIT": "1"}
    readings = [
        subprocess.Popen(
            ["tesseract", str(path), "stdout", "-l", languages, "--psm", SPARSE_TEXT],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env=environment,
        )
        for path in variants(source)
    ]
    for reading in readings:
        text, _ = reading.communicate()
        sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
