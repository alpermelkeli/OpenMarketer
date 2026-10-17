"""Entry point of the studio's sandbox image: run one thing, then report it as one JSON line.

    entry.py python  --timeout S ...           run /in/main.py (working directory /out)
    entry.py render  --width W --height H ...  render /in/page.html to /out/render.png
    entry.py ocr     --image /in/image.png ... read the text in an image

The program (Python, Chromium or Tesseract) runs as a child in a session of its own, with its
output going to files in /tmp, and is killed with its whole group when its time is up. Then the
last line this script prints is the report: the child's exit status, the start of what it
printed, and the regular files in /out (in base64, up to the limits given). The host checks
everything in it again; this script is not the boundary, the container is (see
packages/core/src/openmarketer_core/studio/docker_sandbox.py).
"""

import argparse
import base64
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

OUT = Path("/out")
LOGS = Path("/tmp/sandbox-logs")

CHROMIUM_FLAGS = [
    "--headless=new",
    # Chromium's own sandbox needs privileges the container does not grant; the container
    # is the boundary.
    "--no-sandbox",
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-sync",
    "--no-first-run",
    "--no-default-browser-check",
    "--hide-scrollbars",
    "--mute-audio",
    "--force-device-scale-factor=1",
    "--user-data-dir=/tmp/chromium",
    "--virtual-time-budget=5000",
]


def main() -> int:
    arguments = parse()
    LOGS.mkdir(parents=True, exist_ok=True)
    if arguments.mode == "python":
        command = [sys.executable, "-I", "/opt/studio/run_user.py"]
    elif arguments.mode == "render":
        command = [
            "chromium",
            *CHROMIUM_FLAGS,
            f"--window-size={arguments.width},{arguments.height}",
            "--screenshot=/out/render.png",
            "file:///in/page.html",
        ]
    else:
        command = ["tesseract", arguments.image, "stdout", "-l", "eng"]
    exit_code, timed_out = run(command, arguments.timeout)
    if arguments.mode == "render" and exit_code == 0:
        fit_to(OUT / "render.png", arguments.width, arguments.height)
    stdout, stdout_cut = read_log("stdout", arguments.max_log_chars)
    stderr, stderr_cut = read_log("stderr", arguments.max_log_chars)
    report = {
        "exit_code": exit_code,
        "timed_out": timed_out,
        "stdout": "" if arguments.mode == "ocr" else stdout,
        "stderr": stderr,
        "truncated": stdout_cut or stderr_cut,
        "outputs": [] if arguments.mode == "ocr" else outputs(arguments),
        "text": stdout if arguments.mode == "ocr" else None,
    }
    sys.stdout.write("\n" + json.dumps(report) + "\n")
    sys.stdout.flush()
    return 0


def parse() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["python", "render", "ocr"])
    parser.add_argument("--timeout", type=float, required=True)
    parser.add_argument("--max-files", type=int, required=True)
    parser.add_argument("--max-file-bytes", type=int, required=True)
    parser.add_argument("--max-log-chars", type=int, required=True)
    parser.add_argument("--width", type=int)
    parser.add_argument("--height", type=int)
    parser.add_argument("--image")
    return parser.parse_args()


def run(command: list[str], timeout: float) -> tuple[int, bool]:
    with (LOGS / "stdout").open("wb") as out, (LOGS / "stderr").open("wb") as err:
        child = subprocess.Popen(
            command,
            cwd=OUT,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            start_new_session=True,
        )
        try:
            return child.wait(timeout=timeout), False
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
            return child.returncode, True


def read_log(name: str, limit: int) -> tuple[str, bool]:
    with (LOGS / name).open("rb") as log:
        data = log.read(limit * 4 + 1)
    text = data.decode("utf-8", errors="replace")
    return text[:limit], len(text) > limit or len(data) > limit * 4


def fit_to(path: Path, width: int, height: int) -> None:
    """Crop or pad a screenshot to exactly the size asked; headless window sizes vary."""
    from PIL import Image

    if not path.is_file():
        return
    with Image.open(path) as shot:
        if shot.size == (width, height):
            return
        canvas = Image.new("RGBA", (width, height), (255, 255, 255, 255))
        canvas.paste(shot.crop((0, 0, min(width, shot.width), min(height, shot.height))))
    canvas.save(path, format="PNG")


def outputs(arguments: argparse.Namespace) -> list[dict[str, object]]:
    found = []
    for entry in sorted(os.scandir(OUT), key=lambda e: e.name):
        if not entry.is_file(follow_symlinks=False):
            continue
        size = entry.stat(follow_symlinks=False).st_size
        found.append({"name": entry.name, "size": size, "data": None})
        if len(found) > arguments.max_files:
            break  # one more than allowed, so the host can say so
        if size <= arguments.max_file_bytes:
            with open(entry.path, "rb") as file:
                found[-1]["data"] = base64.b64encode(file.read()).decode("ascii")
    return found


if __name__ == "__main__":
    raise SystemExit(main())
