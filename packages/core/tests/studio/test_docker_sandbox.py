"""How the docker sandbox starts its containers, specified with a runner that starts nothing."""

import base64
import io
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from PIL import Image

from openmarketer_core.studio.docker_sandbox import (
    INSTALL_FAILED,
    NO_REPORT,
    NOT_STARTED,
    CommandOutcome,
    DockerSandbox,
    SandboxSettings,
    installed_modules,
)
from openmarketer_core.studio.sandbox import (
    CanvasSize,
    SandboxFailed,
    SandboxRefused,
    SandboxUnavailable,
)

DRAW = "from PIL import Image\nImage.new('RGB', (64, 64)).save('/out/post.png')\n"
SIZE = CanvasSize(64, 64)


def png(size: tuple[int, int] = (64, 64)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, "white").save(buffer, format="PNG")
    return buffer.getvalue()


def report(
    outputs: Sequence[tuple[str, bytes]] = (), *, exit_code: int = 0, text: str | None = None
) -> bytes:
    return json.dumps(
        {
            "exit_code": exit_code,
            "timed_out": False,
            "stdout": "drawn",
            "stderr": "",
            "truncated": False,
            "outputs": [
                {"name": name, "size": len(data), "data": base64.b64encode(data).decode()}
                for name, data in outputs
            ],
            "text": text,
        }
    ).encode()


@dataclass
class Call:
    args: list[str]
    env: dict[str, str]
    # What the container would find in each bind mount, read while the call runs.
    mounted: dict[str, dict[str, bytes]]


@dataclass
class FakeRunner:
    """Answers each docker command with the next outcome; records what it was given."""

    outcomes: list[CommandOutcome]
    calls: list[Call] = field(default_factory=list)
    on_install: dict[str, bytes] = field(default_factory=dict)

    def __call__(
        self,
        args: Sequence[str],
        *,
        env: Mapping[str, str],
        timeout_s: float,
        max_output_bytes: int,
    ) -> CommandOutcome:
        mounted = {}
        for mount in _values(args, "--mount"):
            parts = dict(part.split("=", 1) for part in mount.split(",") if "=" in part)
            source = Path(parts["source"])
            if parts["target"] == "/deps" and "readonly" not in mount:
                for name, data in self.on_install.items():
                    (source / name).parent.mkdir(parents=True, exist_ok=True)
                    (source / name).write_bytes(data)
            mounted[parts["target"]] = {
                str(path.relative_to(source)): path.read_bytes()
                for path in source.rglob("*")
                if path.is_file()
            }
        self.calls.append(Call(list(args), dict(env), mounted))
        return self.outcomes.pop(0)


def _values(args: Sequence[str], option: str) -> list[str]:
    return [args[i + 1] for i, arg in enumerate(args) if arg == option]


def answered(*stdouts: bytes, returncode: int = 0) -> FakeRunner:
    return FakeRunner([CommandOutcome(returncode, stdout, b"") for stdout in stdouts])


def sandbox(runner: FakeRunner, tmp_path: Path) -> DockerSandbox:
    return DockerSandbox(SandboxSettings(work_root=tmp_path.resolve()), runner)


def run_args(tmp_path: Path, **inputs: bytes) -> list[str]:
    runner = answered(report([("post.png", png())]))
    sandbox(runner, tmp_path).run_python(DRAW, inputs, [], SIZE)
    return runner.calls[0].args


def test_the_run_has_no_network(tmp_path: Path) -> None:
    assert _values(run_args(tmp_path), "--network") == ["none"]


def test_the_run_has_a_read_only_root(tmp_path: Path) -> None:
    assert "--read-only" in run_args(tmp_path)


def test_the_run_drops_every_capability(tmp_path: Path) -> None:
    assert _values(run_args(tmp_path), "--cap-drop") == ["ALL"]


def test_the_run_cannot_gain_privileges(tmp_path: Path) -> None:
    assert _values(run_args(tmp_path), "--security-opt") == ["no-new-privileges"]


def test_the_run_is_not_root(tmp_path: Path) -> None:
    assert _values(run_args(tmp_path), "--user") == ["10001:10001"]


def test_the_run_has_limits(tmp_path: Path) -> None:
    args = run_args(tmp_path)
    limits = [_values(args, option) for option in ("--memory", "--cpus", "--pids-limit")]
    assert limits == [["1024m"], ["1"], ["128"]]


def test_the_run_has_no_swap_beyond_its_memory(tmp_path: Path) -> None:
    assert _values(run_args(tmp_path), "--memory-swap") == ["1024m"]


def test_no_variable_is_passed_to_the_container(tmp_path: Path) -> None:
    args = run_args(tmp_path)
    assert not {"-e", "--env", "--env-file"} & set(args)


def test_the_inputs_are_mounted_read_only(tmp_path: Path) -> None:
    (inputs,) = _values(run_args(tmp_path), "--mount")
    assert inputs.endswith(",target=/in,readonly")


def test_out_is_a_small_tmpfs(tmp_path: Path) -> None:
    tmpfs = _values(run_args(tmp_path), "--tmpfs")
    assert any(t.startswith("/out:") and "size=128m" in t and "noexec" in t for t in tmpfs)


def test_the_image_is_never_pulled(tmp_path: Path) -> None:
    assert _values(run_args(tmp_path), "--pull") == ["never"]


def test_the_container_is_removed_when_it_stops(tmp_path: Path) -> None:
    assert "--rm" in run_args(tmp_path)


def test_docker_runs_without_this_process_s_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-secret-value")
    monkeypatch.setenv("DOCKER_HOST", "unix:///var/run/docker.sock")
    runner = answered(report())
    sandbox(runner, tmp_path).run_python(DRAW, {}, [], SIZE)
    call = runner.calls[0]
    assert "OPENROUTER_API_KEY" not in call.env and call.env["DOCKER_HOST"].startswith("unix:")
    assert not any("sk-secret-value" in arg for arg in call.args)


def test_the_code_inputs_and_canvas_are_written_to_in(tmp_path: Path) -> None:
    runner = answered(report())
    sandbox(runner, tmp_path).run_python(DRAW, {"brand_kit.json": b"{}"}, [], SIZE)
    assert runner.calls[0].mounted["/in"] == {
        "brand_kit.json": b"{}",
        "main.py": DRAW.encode(),
        "canvas.json": b'{"width": 64, "height": 64}',
    }


def test_the_call_s_directory_is_removed(tmp_path: Path) -> None:
    sandbox(answered(report()), tmp_path).run_python(DRAW, {}, [], SIZE)
    assert list(tmp_path.iterdir()) == []


def test_the_images_the_run_left_are_returned(tmp_path: Path) -> None:
    runner = answered(report([("post.png", png())]))
    result = sandbox(runner, tmp_path).run_python(DRAW, {}, [], SIZE)
    assert [(f.name, f.width, f.height) for f in result.files] == [("post.png", 64, 64)]


def test_a_file_that_is_not_an_image_is_not_returned(tmp_path: Path) -> None:
    runner = answered(report([("post.png", png()), ("notes.txt", b"hi")]))
    result = sandbox(runner, tmp_path).run_python(DRAW, {}, [], SIZE)
    assert ([f.name for f in result.files], result.rejected_outputs) == (
        ["post.png"],
        ("notes.txt: the file is not a PNG, JPEG or WebP image",),
    )


def test_a_program_that_fails_is_a_result_with_its_logs(tmp_path: Path) -> None:
    runner = answered(report(exit_code=1))
    result = sandbox(runner, tmp_path).run_python(DRAW, {}, [], SIZE)
    assert (result.succeeded, result.exit_code, result.logs.stdout) == (False, 1, "drawn")


def test_refused_code_starts_no_container(tmp_path: Path) -> None:
    runner = answered()
    with pytest.raises(SandboxRefused) as refused:
        sandbox(runner, tmp_path).run_python("import socket", {}, [], SIZE)
    assert (refused.value.reasons, runner.calls) == (("line 1: importing socket is forbidden",), [])


def test_refused_packages_start_no_container(tmp_path: Path) -> None:
    runner = answered()
    with pytest.raises(SandboxRefused):
        sandbox(runner, tmp_path).run_python(DRAW, {}, ["https://evil.example/x.whl"], SIZE)
    assert runner.calls == []


def test_an_input_may_not_replace_the_code(tmp_path: Path) -> None:
    with pytest.raises(SandboxRefused):
        sandbox(answered(), tmp_path).run_python(DRAW, {"main.py": b"import os"}, [], SIZE)


def test_an_input_name_with_a_directory_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SandboxRefused):
        sandbox(answered(), tmp_path).run_python(DRAW, {"../x": b""}, [], SIZE)


def test_a_run_without_report_fails(tmp_path: Path) -> None:
    with pytest.raises(SandboxFailed, match=NO_REPORT):
        sandbox(answered(b"garbage"), tmp_path).run_python(DRAW, {}, [], SIZE)


def test_docker_that_cannot_start_the_container_is_unavailable(tmp_path: Path) -> None:
    runner = answered(b"", returncode=125)
    with pytest.raises(SandboxUnavailable, match=NOT_STARTED):
        sandbox(runner, tmp_path).run_python(DRAW, {}, [], SIZE)


def test_a_timed_out_container_is_killed(tmp_path: Path) -> None:
    runner = FakeRunner([CommandOutcome(-9, b"", b"", timed_out=True), CommandOutcome(0, b"", b"")])
    with pytest.raises(SandboxFailed):
        sandbox(runner, tmp_path).run_python(DRAW, {}, [], SIZE)
    name = _values(runner.calls[0].args, "--name")[0]
    assert runner.calls[1].args == ["docker", "kill", name]


def install_args(tmp_path: Path) -> list[str]:
    runner = answered(b"", report())
    runner.on_install = {"svgwrite/__init__.py": b""}
    code = "import svgwrite\n"
    sandbox(runner, tmp_path).run_python(code, {}, ["svgwrite==1.4.3"], SIZE)
    return runner.calls[0].args


def test_the_install_is_on_the_proxy_s_network_only(tmp_path: Path) -> None:
    assert _values(install_args(tmp_path), "--network") == ["openmarketer-studio-sandbox"]


def test_the_install_takes_wheels_only(tmp_path: Path) -> None:
    assert "--only-binary=:all:" in install_args(tmp_path)


def test_the_install_goes_through_the_proxy(tmp_path: Path) -> None:
    assert _values(install_args(tmp_path), "--proxy") == ["http://studio-egress-proxy:3128"]


def test_the_install_ignores_pip_s_environment_and_configuration(tmp_path: Path) -> None:
    assert "--isolated" in install_args(tmp_path)


def test_the_packages_come_after_the_end_of_options(tmp_path: Path) -> None:
    args = install_args(tmp_path)
    assert args[args.index("--") + 1 :] == ["svgwrite==1.4.3"]


def test_the_install_is_locked_down_like_the_run(tmp_path: Path) -> None:
    args = install_args(tmp_path)
    assert {"--read-only", "--rm"} <= set(args) and _values(args, "--cap-drop") == ["ALL"]


def test_the_installed_packages_are_mounted_read_only_into_the_run(tmp_path: Path) -> None:
    runner = answered(b"", report())
    runner.on_install = {"svgwrite/__init__.py": b"# svg"}
    sandbox(runner, tmp_path).run_python("import svgwrite\n", {}, ["svgwrite"], SIZE)
    run = runner.calls[1]
    assert any(m.endswith(",target=/deps,readonly") for m in _values(run.args, "--mount"))
    assert run.mounted["/deps"] == {"svgwrite/__init__.py": b"# svg"}


def test_code_importing_what_was_not_installed_is_refused_after_the_install(
    tmp_path: Path,
) -> None:
    runner = answered(b"")
    runner.on_install = {"svgwrite/__init__.py": b""}
    with pytest.raises(SandboxRefused):
        sandbox(runner, tmp_path).run_python("import matplotlib\n", {}, ["svgwrite"], SIZE)
    assert len(runner.calls) == 1


def test_a_failed_install_is_a_fixed_sentence(tmp_path: Path) -> None:
    runner = FakeRunner([CommandOutcome(1, b"", b"ERROR: no matching distribution")])
    with pytest.raises(SandboxFailed) as failed:
        sandbox(runner, tmp_path).run_python("import svgwrite\n", {}, ["svgwrite"], SIZE)
    assert (str(failed.value), failed.value.logs.stderr) == (
        INSTALL_FAILED,
        "ERROR: no matching distribution",
    )


def test_installed_modules_are_the_top_level_packages_and_modules(tmp_path: Path) -> None:
    for name in ("svgwrite/__init__.py", "svgwrite-1.4.3.dist-info/METADATA", "six.py", "bin/x"):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes(b"")
    assert installed_modules(tmp_path) == {"svgwrite", "six"}


def test_a_render_is_asked_at_its_size(tmp_path: Path) -> None:
    runner = answered(report([("render.png", png((200, 100)))]))
    result = sandbox(runner, tmp_path).render_html("<p>hi</p>", {}, 200, 100)
    args = runner.calls[0].args
    assert (_values(args, "--width"), _values(args, "--height")) == (["200"], ["100"])
    assert [(f.width, f.height) for f in result.files] == [(200, 100)]


def test_a_render_of_the_wrong_size_is_not_returned(tmp_path: Path) -> None:
    runner = answered(report([("render.png", png((64, 64)))]))
    result = sandbox(runner, tmp_path).render_html("<p>hi</p>", {}, 200, 100)
    assert result.files == () and result.rejected_outputs


def test_the_page_is_written_to_in(tmp_path: Path) -> None:
    runner = answered(report([("render.png", png((200, 100)))]))
    sandbox(runner, tmp_path).render_html("<p>hi</p>", {"logo.png": png()}, 200, 100)
    assert set(runner.calls[0].mounted["/in"]) == {"page.html", "logo.png"}


def test_ocr_returns_the_text_read(tmp_path: Path) -> None:
    runner = answered(report(text="HELLO\n"))
    assert sandbox(runner, tmp_path).ocr(png()) == "HELLO\n"


def test_ocr_of_what_is_not_an_image_starts_no_container(tmp_path: Path) -> None:
    runner = answered()
    with pytest.raises(SandboxRefused):
        sandbox(runner, tmp_path).ocr(b"not an image")
    assert runner.calls == []
