"""The ``Sandbox`` port run in Docker containers, through the docker command line.

Each call gets a directory of its own on this machine, removed when it returns, and at most two
containers, both removed when they stop:

1. Install, only when the model asked for extra packages and screening accepted them. pip runs
   in the sandbox image, installing wheels only (``--only-binary=:all:``: no package's build
   code runs) into the call's directory. The container's one network is an internal Docker
   network whose only other member is an egress proxy that lets through HTTPS to pypi.org and
   files.pythonhosted.org and nothing else (``deploy/compose/dev.yml``, profile ``studio``).
   What was installed is then mounted read-only into the run.
2. Run. The code, which screening read first (``code_screening.py``), runs with no network at
   all, a read-only root file system, every capability dropped, no way to gain privileges, a
   user that is not root, limits on CPU, memory, processes and time, its inputs read-only in
   ``/in`` and a small in-memory ``/out`` for what it makes. The image's entry script runs the
   program, then reports in one line of JSON what it printed and the files it left; only the
   files ``output_check.py`` accepts as images come out.

No secret can reach a container: docker runs with an environment built here from a short list
(what it needs to find its daemon), and no ``--env`` is ever passed to a container. docker is
given a list of arguments, never a shell line, and nothing the model wrote becomes an argument
except package names that passed screening, after ``--``.

What a container prints is the run's (or pip's, or PyPI's) text. It is returned as logs, cut
to a fixed length, for the audit log; an error raised here is a fixed sentence.

An optional runtime (gVisor's ``runsc``) can be set for a stronger boundary than the kernel's
namespaces; the default is Docker's own.
"""

import base64
import binascii
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

from openmarketer_core.studio.code_screening import (
    Requirement,
    screen_code,
    screen_requirements,
)
from openmarketer_core.studio.output_check import (
    OutputLimits,
    RawOutput,
    check_image,
    check_outputs,
)
from openmarketer_core.studio.sandbox import (
    CanvasSize,
    SandboxFailed,
    SandboxLogs,
    SandboxRefused,
    SandboxResult,
    SandboxUnavailable,
)

logger = logging.getLogger(__name__)

# The user the sandbox image runs as (deploy/sandbox/studio/Dockerfile).
SANDBOX_USER = "10001:10001"

# The variables of this process the docker command line may see: what it needs to find and
# reach its daemon. Everything else, every secret among it, is withheld.
INHERITED_VARIABLES = (
    "PATH",
    "HOME",  # the docker client's own configuration and contexts are under it
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_CONFIG",
    "DOCKER_CERT_PATH",
    "DOCKER_TLS_VERIFY",
    "XDG_RUNTIME_DIR",  # where a rootless daemon's socket is
)

# docker's own exit status when it could not start the container (no daemon, no image, no
# network of that name, a refused option).
_DOCKER_COULD_NOT_START = 125

# Files in /in that a call writes itself; an input may not take their names.
_RESERVED_INPUTS = frozenset({"main.py", "page.html", "canvas.json"})
_INPUT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
# Only these characters in a directory handed to ``--mount``, whose value is comma-separated.
_MOUNTABLE_PATH = re.compile(r"[A-Za-z0-9/._-]+")
_EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}

NOT_STARTED = (
    "the sandbox could not be started: docker, the sandbox image or its network is missing"
)
NO_DOCKER = "the sandbox could not be started: the docker command was not found"
NO_REPORT = "the sandbox did not report what the run did"
INSTALL_FAILED = "the requested packages could not be installed (only wheels from PyPI are used)"
INSTALL_TOO_LARGE = "the requested packages are larger than the sandbox allows"
NOT_A_PATH_FOR_DOCKER = "the sandbox's work directory has characters docker cannot mount"


def _did_not_finish(what: str, seconds: float) -> str:
    return f"{what} did not finish within {seconds:g} seconds"


@dataclass(frozen=True)
class SandboxSettings:
    image: str = "openmarketer/studio-sandbox:dev"
    run_timeout_s: float = 60
    install_timeout_s: float = 180
    # What a container may take beyond its own time limit to start and stop.
    startup_margin_s: float = 30
    memory_mb: int = 1024
    cpus: float = 1.0
    pids: int = 128
    out_size_mb: int = 128
    tmp_size_mb: int = 256
    install_tmp_size_mb: int = 512
    # The internal network the install phase is on, and the proxy on it.
    proxy_network: str = "openmarketer-studio-sandbox"
    proxy_url: str = "http://studio-egress-proxy:3128"
    index_url: str = "https://pypi.org/simple"
    max_install_bytes: int = 300 * 1024 * 1024
    max_inputs: int = 16
    max_input_bytes: int = 50 * 1024 * 1024
    max_log_chars: int = 8_000
    max_ocr_chars: int = 20_000
    max_canvas_side: int = 4_096
    outputs: OutputLimits = field(default_factory=OutputLimits)
    # A container runtime of a stronger boundary, such as gVisor's ``runsc``; None is docker's.
    runtime: str | None = None
    docker: str = "docker"
    # Where each call's directory is made; None is the system's temporary directory.
    work_root: Path | None = None

    @property
    def max_report_bytes(self) -> int:
        """The most a run's report may be: its images in base64, its logs, some room."""
        images = self.outputs.max_total_bytes * 4 // 3
        return images + 8 * self.max_log_chars + self.max_ocr_chars + 1024 * 1024


@dataclass(frozen=True)
class CommandOutcome:
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    # The command printed more than it was allowed to and was stopped.
    overflowed: bool = False


class CommandRunner(Protocol):
    def __call__(
        self,
        args: Sequence[str],
        *,
        env: Mapping[str, str],
        timeout_s: float,
        max_output_bytes: int,
    ) -> CommandOutcome: ...


def run_command(
    args: Sequence[str],
    *,
    env: Mapping[str, str],
    timeout_s: float,
    max_output_bytes: int,
) -> CommandOutcome:
    """Run ``args`` (no shell), keeping at most ``max_output_bytes`` of each stream."""
    process = subprocess.Popen(
        list(args),
        env=dict(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    overflow = threading.Event()
    assert process.stdout is not None and process.stderr is not None
    readers = [
        _BoundedReader(process.stdout, max_output_bytes, overflow, process),
        _BoundedReader(process.stderr, max_output_bytes, overflow, process),
    ]
    for reader in readers:
        reader.start()
    timed_out = False
    try:
        process.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        process.wait()
    for reader in readers:
        reader.join()
    return CommandOutcome(
        returncode=process.returncode,
        stdout=bytes(readers[0].kept),
        stderr=bytes(readers[1].kept),
        timed_out=timed_out,
        overflowed=overflow.is_set(),
    )


class _BoundedReader(threading.Thread):
    def __init__(
        self,
        stream: IO[bytes],
        limit: int,
        overflow: threading.Event,
        process: subprocess.Popen[bytes],
    ) -> None:
        super().__init__(daemon=True)
        self.stream = stream
        self.limit = limit
        self.overflow = overflow
        self.process = process
        self.kept = bytearray()

    def run(self) -> None:
        while chunk := self.stream.read(65536):
            if len(self.kept) + len(chunk) > self.limit:
                self.overflow.set()
                self.process.kill()
                break
            self.kept.extend(chunk)
        self.stream.close()


class _ReportedOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    size: int
    data: str | None


class _Report(BaseModel):
    """What the image's entry script prints as its last line."""

    model_config = ConfigDict(extra="forbid")

    exit_code: int
    timed_out: bool
    stdout: str
    stderr: str
    truncated: bool
    outputs: list[_ReportedOutput]
    text: str | None = None


@dataclass(frozen=True)
class _Installed:
    directory: Path
    modules: frozenset[str]


class DockerSandbox:
    """The studio's sandbox, run in containers of the sandbox image."""

    def __init__(self, settings: SandboxSettings, runner: CommandRunner = run_command) -> None:
        self.settings = settings
        self._runner = runner

    def run_python(
        self,
        code: str,
        inputs: Mapping[str, bytes],
        requirements: Sequence[str],
        size: CanvasSize,
    ) -> SandboxResult:
        packages = screen_requirements(requirements)
        if not packages.accepted:
            raise SandboxRefused(packages.reasons)
        self._check_canvas(size.width, size.height)
        self._check_inputs(inputs)
        first_look = screen_code(code, unlisted_imports_allowed=bool(packages.requirements))
        if not first_look.accepted:
            raise SandboxRefused(first_look.reasons)
        with self._work_directory() as work:
            installed = (
                self._install(work, packages.requirements) if packages.requirements else None
            )
            verdict = screen_code(code, installed_modules=installed.modules if installed else ())
            if not verdict.accepted:
                raise SandboxRefused(verdict.reasons)
            return self._run_phase(work, code, inputs, size, installed)

    def render_html(
        self, html: str, inputs: Mapping[str, bytes], width: int, height: int
    ) -> SandboxResult:
        self._check_canvas(width, height)
        self._check_inputs(inputs)
        with self._work_directory() as work:
            given = self._write_inputs(work, inputs, {"page.html": html.encode("utf-8")})
            command = ["render", "--width", str(width), "--height", str(height)]
            report, duration = self._run_container(work, given, None, command)
            result = self._result(report, duration)
            wrong_size = [
                f"{file.name}: rendered at {file.width}x{file.height}, not {width}x{height}"
                for file in result.files
                if (file.width, file.height) != (width, height)
            ]
            if not wrong_size:
                return result
            kept = tuple(
                file for file in result.files if (file.width, file.height) == (width, height)
            )
            return SandboxResult(
                files=kept,
                logs=result.logs,
                duration_s=result.duration_s,
                exit_code=result.exit_code,
                timed_out=result.timed_out,
                rejected_outputs=result.rejected_outputs + tuple(wrong_size),
            )

    def ocr(self, image: bytes) -> str:
        checked, reason = check_image(RawOutput("image", image), self.settings.outputs)
        if checked is None:
            raise SandboxRefused([f"the image to read: {reason}"])
        name = f"image.{_EXTENSIONS[checked.mime]}"
        with self._work_directory() as work:
            given = self._write_inputs(work, {}, {name: image})
            report, _ = self._run_container(work, given, None, ["ocr", "--image", f"/in/{name}"])
        if report.exit_code != 0 or report.text is None:
            raise SandboxFailed("the text in the image could not be read", self._logs(report))
        return report.text[: self.settings.max_ocr_chars]

    def _run_phase(
        self,
        work: Path,
        code: str,
        inputs: Mapping[str, bytes],
        size: CanvasSize,
        installed: _Installed | None,
    ) -> SandboxResult:
        canvas = json.dumps({"width": size.width, "height": size.height}).encode()
        own = {"main.py": code.encode("utf-8"), "canvas.json": canvas}
        given = self._write_inputs(work, inputs, own)
        deps = installed.directory if installed else None
        report, duration = self._run_container(work, given, deps, ["python"])
        return self._result(report, duration)

    def _install(self, work: Path, requirements: Sequence[Requirement]) -> _Installed:
        settings = self.settings
        target = work / "deps"
        target.mkdir()
        user = _install_user()
        if user == SANDBOX_USER:
            target.chmod(0o777)  # this process is root; the container's user must write here
        args = [
            *self._run_prefix(user=user, network=settings.proxy_network),
            "--tmpfs",
            f"/tmp:rw,nosuid,nodev,size={settings.install_tmp_size_mb}m",
            "--mount",
            _bind_mount(target, "/deps", read_only=False),
            "--entrypoint",
            "python",
            settings.image,
            "-I",
            "-m",
            "pip",
            "install",
            "--isolated",
            "--no-input",
            "--disable-pip-version-check",
            "--progress-bar",
            "off",
            "--only-binary=:all:",
            "--no-cache-dir",
            "--target",
            "/deps",
            "--index-url",
            settings.index_url,
            "--proxy",
            settings.proxy_url,
            "--",
            *(requirement.pip_argument() for requirement in requirements),
        ]
        outcome = self._docker(args, settings.install_timeout_s)
        logs = _command_logs(outcome, settings.max_log_chars)
        if outcome.timed_out:
            raise SandboxFailed(
                _did_not_finish("installing the packages", settings.install_timeout_s), logs
            )
        if outcome.returncode != 0:
            logger.info("sandbox install failed: %s", logs.stderr)
            raise SandboxFailed(INSTALL_FAILED, logs)
        if _tree_bytes(target) > settings.max_install_bytes:
            raise SandboxFailed(INSTALL_TOO_LARGE, logs)
        return _Installed(target, installed_modules(target))

    def _run_container(
        self,
        work: Path,
        given: Path,
        deps: Path | None,
        command: Sequence[str],
    ) -> tuple[_Report, float]:
        settings = self.settings
        limits = settings.outputs
        args = [
            *self._run_prefix(user=SANDBOX_USER, network="none"),
            "--tmpfs",
            f"/tmp:rw,nosuid,nodev,size={settings.tmp_size_mb}m",
            "--tmpfs",
            f"/out:rw,nosuid,nodev,noexec,size={settings.out_size_mb}m,uid=10001,gid=10001,mode=0700",
            "--mount",
            _bind_mount(given, "/in", read_only=True),
        ]
        if deps is not None:
            args += ["--mount", _bind_mount(deps, "/deps", read_only=True)]
        args += [
            "--workdir",
            "/out",
            settings.image,
            *command,
            "--timeout",
            f"{settings.run_timeout_s:g}",
            "--max-files",
            str(limits.max_files),
            "--max-file-bytes",
            str(limits.max_file_bytes),
            "--max-log-chars",
            str(settings.max_log_chars),
        ]
        started = time.monotonic()
        outcome = self._docker(args, settings.run_timeout_s)
        duration = time.monotonic() - started
        logs = _command_logs(outcome, settings.max_log_chars)
        if outcome.timed_out:
            raise SandboxFailed(_did_not_finish("the sandbox", settings.run_timeout_s), logs)
        if outcome.overflowed:
            raise SandboxFailed("the sandbox printed more than it is allowed to", logs)
        report = _read_report(outcome.stdout)
        if report is None:
            logger.info("sandbox gave no report (exit %s): %s", outcome.returncode, logs.stderr)
            raise SandboxFailed(NO_REPORT, logs)
        return report, duration

    def _run_prefix(self, *, user: str, network: str) -> list[str]:
        """The options every container is started with; the call adds its mounts."""
        settings = self.settings
        prefix = [
            settings.docker,
            "run",
            "--rm",
            "--name",
            f"openmarketer-studio-{uuid.uuid4().hex[:16]}",
            "--label",
            "openmarketer.studio-sandbox=1",
            "--pull",
            "never",
            "--network",
            network,
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            user,
            "--memory",
            f"{settings.memory_mb}m",
            "--memory-swap",
            f"{settings.memory_mb}m",
            "--cpus",
            f"{settings.cpus:g}",
            "--pids-limit",
            str(settings.pids),
            "--ulimit",
            "nofile=256:256",
            "--ipc",
            "private",
        ]
        if settings.runtime is not None:
            prefix += ["--runtime", settings.runtime]
        return prefix

    def _docker(self, args: list[str], timeout_s: float) -> CommandOutcome:
        name = args[args.index("--name") + 1]
        try:
            outcome = self._runner(
                args,
                env=docker_environment(os.environ),
                timeout_s=timeout_s + self.settings.startup_margin_s,
                max_output_bytes=self.settings.max_report_bytes,
            )
        except FileNotFoundError:
            raise SandboxUnavailable(NO_DOCKER) from None
        if outcome.timed_out or outcome.overflowed:
            self._kill(name)
        if outcome.returncode == _DOCKER_COULD_NOT_START and not outcome.timed_out:
            logger.warning("docker could not start the sandbox: %s", _text(outcome.stderr, 2000))
            raise SandboxUnavailable(NOT_STARTED)
        return outcome

    def _kill(self, name: str) -> None:
        """Stop a container whose docker client was stopped: killing the client does not."""
        outcome = self._runner(
            [self.settings.docker, "kill", name],
            env=docker_environment(os.environ),
            timeout_s=30,
            max_output_bytes=64 * 1024,
        )
        if outcome.returncode != 0:
            logger.warning("the sandbox container %s may still be running", name)

    def _result(self, report: _Report, duration: float) -> SandboxResult:
        raw: list[RawOutput] = []
        too_large: list[str] = []
        for output in report.outputs:
            data = _decoded(output.data)
            if data is None:
                too_large.append(
                    f"{output.name[:100]}: not returned by the sandbox "
                    "(too large, unreadable or beyond the number of files allowed)"
                )
                continue
            raw.append(RawOutput(output.name, data))
        checked = check_outputs(raw, self.settings.outputs)
        return SandboxResult(
            files=checked.files,
            logs=self._logs(report),
            duration_s=duration,
            exit_code=report.exit_code,
            timed_out=report.timed_out,
            rejected_outputs=tuple(too_large) + checked.rejected,
        )

    def _logs(self, report: _Report) -> SandboxLogs:
        limit = self.settings.max_log_chars
        cut = len(report.stdout) > limit or len(report.stderr) > limit
        return SandboxLogs(
            stdout=report.stdout[:limit],
            stderr=report.stderr[:limit],
            truncated=report.truncated or cut,
        )

    def _check_canvas(self, width: int, height: int) -> None:
        side = self.settings.max_canvas_side
        if not (16 <= width <= side and 16 <= height <= side):
            raise SandboxRefused([f"the canvas must be between 16 and {side} pixels a side"])

    def _check_inputs(self, inputs: Mapping[str, bytes]) -> None:
        settings = self.settings
        reasons: list[str] = []
        if len(inputs) > settings.max_inputs:
            reasons.append(f"at most {settings.max_inputs} inputs may be given")
        if sum(len(data) for data in inputs.values()) > settings.max_input_bytes:
            reasons.append(f"the inputs exceed {settings.max_input_bytes} bytes")
        for name in inputs:
            if not _INPUT_NAME.fullmatch(name):
                reasons.append("an input's name is not a plain file name")
            elif name in _RESERVED_INPUTS:
                reasons.append(f"an input may not be named {name}")
        if reasons:
            raise SandboxRefused(list(dict.fromkeys(reasons)))

    def _write_inputs(
        self, work: Path, inputs: Mapping[str, bytes], own: Mapping[str, bytes]
    ) -> Path:
        given = work / "in"
        given.mkdir()
        given.chmod(0o755)  # read by the container's user, which is not this process's
        for name, data in {**inputs, **own}.items():
            path = given / name
            path.write_bytes(data)
            path.chmod(0o644)
        return given

    def _work_directory(self) -> "_WorkDirectory":
        return _WorkDirectory(self.settings.work_root)


class _WorkDirectory:
    """A directory for one call, removed when the call returns, whatever happened."""

    def __init__(self, root: Path | None) -> None:
        self._directory = tempfile.TemporaryDirectory(
            prefix="openmarketer-studio-", dir=root, ignore_cleanup_errors=True
        )

    def __enter__(self) -> Path:
        path = Path(self._directory.name).resolve()
        if not _MOUNTABLE_PATH.fullmatch(str(path)):
            self._directory.cleanup()
            raise SandboxUnavailable(NOT_A_PATH_FOR_DOCKER)
        return path

    def __exit__(self, *_: object) -> None:
        self._directory.cleanup()


def docker_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """The environment the docker command line runs with, built from ``INHERITED_VARIABLES``."""
    return {name: environment[name] for name in INHERITED_VARIABLES if name in environment}


def installed_modules(target: Path) -> frozenset[str]:
    """The top-level modules pip installed into ``target``: packages, modules, extensions."""
    names: set[str] = set()
    for entry in target.iterdir():
        if entry.is_symlink() or entry.name.startswith((".", "_")):
            continue
        if entry.is_dir():
            if entry.suffix in {".dist-info", ".data"} or entry.name == "bin":
                continue
            if "." not in entry.name:
                names.add(entry.name)
        elif entry.suffix in {".py", ".so", ".pyd"}:
            names.add(entry.name.split(".")[0])
    return frozenset(names)


def _install_user() -> str:
    """Who pip runs as: this process's user, so that this process can remove what it wrote."""
    uid, gid = os.getuid(), os.getgid()
    return SANDBOX_USER if uid == 0 else f"{uid}:{gid}"


def _bind_mount(source: Path, target: str, *, read_only: bool) -> str:
    if not _MOUNTABLE_PATH.fullmatch(str(source)):
        raise SandboxUnavailable(NOT_A_PATH_FOR_DOCKER)
    mount = f"type=bind,source={source},target={target}"
    return mount + ",readonly" if read_only else mount


def _tree_bytes(root: Path) -> int:
    total = 0
    for directory, _, files in os.walk(root):
        for name in files:
            total += os.lstat(os.path.join(directory, name)).st_size
    return total


def _read_report(stdout: bytes) -> _Report | None:
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        return None
    try:
        return _Report.model_validate(json.loads(lines[-1]))
    except (ValueError, ValidationError):
        return None


def _decoded(data: str | None) -> bytes | None:
    if data is None:
        return None
    try:
        return base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        return None


def _command_logs(outcome: CommandOutcome, limit: int) -> SandboxLogs:
    stdout, stderr = _text(outcome.stdout, limit), _text(outcome.stderr, limit)
    cut = len(outcome.stdout) > limit or len(outcome.stderr) > limit or outcome.overflowed
    return SandboxLogs(stdout=stdout, stderr=stderr, truncated=cut)


def _text(data: bytes, limit: int) -> str:
    return data[: limit * 4].decode("utf-8", errors="replace")[:limit]
