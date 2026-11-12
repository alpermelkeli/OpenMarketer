"""The docker sandbox run for real: containers of the sandbox image on this machine.

Skipped unless docker is on the PATH, its daemon answers and the image is built
(``make sandbox``). Nothing here uses the network: the run phase has none, and no test installs
a package.
"""

import io
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from openmarketer_core.studio.docker_sandbox import DockerSandbox, SandboxSettings
from openmarketer_core.studio.image_checks import copy_matches
from openmarketer_core.studio.ocr_languages import languages_for
from openmarketer_core.studio.sandbox import CanvasSize

IMAGE = SandboxSettings().image


def _image_is_built() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        found = subprocess.run(
            ["docker", "image", "inspect", IMAGE], capture_output=True, timeout=20, check=False
        )
    except subprocess.TimeoutExpired:
        return False
    return found.returncode == 0


pytestmark = pytest.mark.skipif(not _image_is_built(), reason=f"needs docker and {IMAGE}")


@pytest.fixture
def sandbox(tmp_path: Path) -> DockerSandbox:
    return DockerSandbox(SandboxSettings(run_timeout_s=60, work_root=tmp_path.resolve()))


def test_a_pillow_program_makes_a_png(sandbox: DockerSandbox) -> None:
    code = (
        "import json\n"
        "from PIL import Image\n"
        "canvas = json.load(open('/in/canvas.json'))\n"
        "Image.new('RGB', (canvas['width'], canvas['height']), 'teal').save('/out/post.png')\n"
        "print('done')\n"
    )
    result = sandbox.run_python(code, {}, [], CanvasSize(320, 200))
    assert result.succeeded, result.logs
    assert [(f.name, f.mime, f.width, f.height) for f in result.files] == [
        ("post.png", "image/png", 320, 200)
    ]
    assert result.logs.stdout == "done\n"


def test_a_socket_cannot_reach_anything_from_a_run(sandbox: DockerSandbox) -> None:
    # Screening would refuse this code; the run phase is called directly to show that the
    # container holds without it.
    code = (
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 443), timeout=3)\n"
        "    print('connected')\n"
        "except OSError as e:\n"
        "    print('refused', type(e).__name__)\n"
    )
    with sandbox._work_directory() as work:  # pyright: ignore[reportPrivateUsage]
        result = sandbox._run_phase(work, code, {}, CanvasSize(64, 64), None)  # pyright: ignore[reportPrivateUsage]
    assert result.logs.stdout.startswith("refused"), result.logs


def test_the_run_sees_none_of_this_process_s_variables(
    sandbox: DockerSandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-should-not-be-seen")
    code = "import os\nprint(sorted(os.environ))\nprint(os.environ.get('OPENROUTER_API_KEY'))\n"
    result = sandbox.run_python(code, {}, [], CanvasSize(64, 64))
    assert "sk-should-not-be-seen" not in result.logs.stdout
    assert result.logs.stdout.endswith("None\n")


def test_the_inputs_cannot_be_written(sandbox: DockerSandbox) -> None:
    code = (
        "try:\n"
        "    open('/in/brand_kit.json', 'w').write('{}')\n"
        "    print('written')\n"
        "except OSError:\n"
        "    print('read-only')\n"
    )
    result = sandbox.run_python(code, {"brand_kit.json": b'{"a": 1}'}, [], CanvasSize(64, 64))
    assert result.logs.stdout == "read-only\n"


def test_a_program_over_its_time_is_stopped(tmp_path: Path) -> None:
    quick = DockerSandbox(SandboxSettings(run_timeout_s=2, work_root=tmp_path.resolve()))
    result = quick.run_python("while True:\n    pass\n", {}, [], CanvasSize(64, 64))
    assert result.timed_out and not result.succeeded


def test_html_is_rendered_to_a_png_of_its_size(sandbox: DockerSandbox) -> None:
    html = "<html><body style='margin:0;background:#2a6'><h1>Launch</h1></body></html>"
    result = sandbox.render_html(html, {}, 600, 314)
    assert result.succeeded, result.logs
    assert [(f.name, f.width, f.height) for f in result.files] == [("render.png", 600, 314)]


def test_ocr_reads_known_text(sandbox: DockerSandbox) -> None:
    image = Image.new("RGB", (900, 200), "white")
    font = ImageFont.load_default(size=72)
    ImageDraw.Draw(image).text((40, 50), "OPEN MARKETER", fill="black", font=font)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    assert "OPEN MARKETER" in sandbox.ocr(buffer.getvalue(), ["eng"])


def test_every_face_of_the_catalogue_is_in_the_image_and_renders_turkish() -> None:
    checked = subprocess.run(
        [
            "docker", "run", "--rm", "--network", "none", "--read-only",
            "--entrypoint", "python", IMAGE,
            "-I", "/opt/studio/check_fonts.py", "/opt/studio/lib/typefaces.json",
        ],
        capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip
    assert checked.returncode == 0, checked.stdout + checked.stderr


def test_a_program_fits_words_in_a_box_with_studio_type(sandbox: DockerSandbox) -> None:
    code = (
        "from PIL import Image\n"
        "from studio_type import fit_text\n"
        "image = Image.new('RGB', (800, 400), 'white')\n"
        "fitted = fit_text(image, 'Pazarı kim yapacak?', (40, 40, 760, 360), 'League Spartan',\n"
        "                  'bold', fill='black')\n"
        "left, top, right, bottom = fitted.bbox\n"
        "print(fitted.size, 40 <= left and right <= 760 and 40 <= top and bottom <= 360)\n"
        "image.save('/out/image.png')\n"
    )
    result = sandbox.run_python(code, {}, [], CanvasSize(800, 400))
    assert result.succeeded, result.logs
    size, inside = result.logs.stdout.split()
    assert int(size) > 60 and inside == "True"


def test_words_studio_type_sets_in_a_catalogue_font_are_read_in_turkish(
    sandbox: DockerSandbox,
) -> None:
    copy = ["Ürünü sen yaptın.", "Pazarı kim yapacak?"]
    code = (
        "from PIL import Image\n"
        "from studio_type import fit_text\n"
        "image = Image.new('RGB', (1200, 600), (20, 24, 40))\n"
        f"fit_text(image, {copy[0]!r}, (60, 80, 1140, 180), 'Inter', 'bold', fill='white')\n"
        f"fit_text(image, {copy[1]!r}, (60, 320, 1140, 420), 'Vollkorn', fill='white')\n"
        "image.save('/out/image.png')\n"
    )
    made = sandbox.run_python(code, {}, [], CanvasSize(1200, 600))
    assert made.succeeded, made.logs
    text = sandbox.ocr(made.files[0].data, languages_for(copy))
    assert copy_matches(text, copy).all_confirmed, text
