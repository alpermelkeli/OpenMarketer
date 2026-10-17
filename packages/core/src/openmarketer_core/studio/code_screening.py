"""The first look at what the studio's model asks the sandbox to run: its Python and its packages.

``screen_code`` reads model-written Python as a syntax tree, never runs it, and refuses what
has no place in composing an image: importing a module outside a short list (the libraries
the sandbox image holds, a few harmless parts of the standard library, and the packages
installed for this run), the modules that start processes, open sockets or load native code,
the built-ins that run text as code, the attributes that walk from any object to the
interpreter's internals, and a file opened outside ``/in`` and ``/out`` when its path is
written in the code. ``screen_requirements`` reads the extra packages the model asks for:
a name and at most an exact version, nothing pip would read as an option, a URL or a path.

Screening is a first filter, not the boundary. Python is too dynamic for a reading of its
source to prove what it does, and nothing here tries: a path built at run time, or a trick no
rule names, passes. What contains the code is the container it runs in (no network, no
secrets, a read-only file system, limits; see ``docker_sandbox.py``). Screening exists so that
the plain cases are refused early, with a reason the model can act on, and so that the audit
log shows what was refused.

This module is pure: it imports no framework, starts nothing and reads no file.
"""

import ast
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

# The most source the sandbox is given to run.
MAX_SOURCE_BYTES = 64 * 1024
# The most extra packages one run may ask for.
MAX_REQUIREMENTS = 5

# What the sandbox image installs (deploy/sandbox/studio/requirements.txt), as imported.
PREINSTALLED_MODULES = frozenset({"PIL", "numpy", "cairosvg", "qrcode", "fontTools"})

# The parts of the standard library composition needs and that reach nothing outside the
# process. ``os`` is here for ``os.path``; what in it starts or replaces a process is refused
# by name (``FORBIDDEN_OS_FUNCTIONS``).
SAFE_STDLIB_MODULES = frozenset(
    {
        "base64",
        "bisect",
        "collections",
        "colorsys",
        "copy",
        "dataclasses",
        "decimal",
        "enum",
        "fractions",
        "functools",
        "heapq",
        "io",
        "itertools",
        "json",
        "math",
        "operator",
        "os",
        "pathlib",
        "random",
        "re",
        "statistics",
        "string",
        "textwrap",
        "typing",
        "unicodedata",
    }
)

# Never importable, whatever was installed: each starts a process, opens a connection, loads
# native code or imports by a name chosen at run time.
FORBIDDEN_MODULES = frozenset(
    {
        "asyncio",
        "builtins",
        "ctypes",
        "http",
        "importlib",
        "multiprocessing",
        "pty",
        "requests",
        "selectors",
        "shutil",
        "signal",
        "socket",
        "ssl",
        "subprocess",
        "sys",
        "threading",
        "urllib",
    }
)

FORBIDDEN_BUILTINS = frozenset(
    {"__import__", "breakpoint", "compile", "eval", "exec", "globals", "locals", "vars"}
)

FORBIDDEN_OS_FUNCTIONS = frozenset(
    {
        "execl",
        "execle",
        "execlp",
        "execlpe",
        "execv",
        "execve",
        "execvp",
        "execvpe",
        "fork",
        "forkpty",
        "kill",
        "killpg",
        "popen",
        "posix_spawn",
        "posix_spawnp",
        "putenv",
        "spawnl",
        "spawnle",
        "spawnlp",
        "spawnlpe",
        "spawnv",
        "spawnve",
        "spawnvp",
        "spawnvpe",
        "system",
    }
)

# The dunder attributes ordinary code uses. Every other one is refused: they are how code
# walks from a value to the interpreter's internals (``().__class__.__bases__[0]
# .__subclasses__()``, ``f.__globals__["__builtins__"]``).
ALLOWED_DUNDER_ATTRIBUTES = frozenset({"__init__", "__name__", "__doc__"})
# Attributes of frames, generators and code objects, which reach the same internals.
FORBIDDEN_ATTRIBUTES = frozenset(
    {
        "co_code",
        "cr_frame",
        "f_back",
        "f_builtins",
        "f_globals",
        "f_locals",
        "gi_code",
        "gi_frame",
        "tb_frame",
    }
)
FORBIDDEN_NAMES = frozenset({"__builtins__", "__loader__", "__spec__"})

# Where a file written into the code may be: the inputs, the outputs, the image's fonts.
ALLOWED_PATH_ROOTS = ("/in", "/out", "/usr/share/fonts")
# The calls whose first argument is a path.
_PATH_CALLS = frozenset({"open", "Path", "PurePath", "PosixPath"})
_ATTRIBUTE_ACCESS_CALLS = frozenset({"getattr", "setattr", "delattr", "hasattr"})

# A distribution name as PEP 508 allows it, and at most ``==`` and a plain release version.
_REQUIREMENT = re.compile(
    r"(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]{0,62}[A-Za-z0-9])?)"
    r"(?:==(?P<version>[0-9]{1,6}(?:\.[0-9]{1,6}){0,4}"
    r"(?:(?:a|b|rc)[0-9]{1,4})?(?:\.post[0-9]{1,4})?))?"
)
_NAME_SEPARATORS = re.compile(r"[-_.]+")

# Packages a run may not ask for, by normalized name: the installer's own tools, which would
# replace the code that installs; the libraries the image already holds; and those whose only
# purpose is the network, other processes or native code, which a run has no use for.
DENIED_REQUIREMENTS = frozenset(
    {
        "aiohttp",
        "boto3",
        "cairosvg",
        "cffi",
        "distribute",
        "docker",
        "fonttools",
        "httpx",
        "numpy",
        "paramiko",
        "pexpect",
        "pillow",
        "pip",
        "playwright",
        "plumbum",
        "psutil",
        "pyautogui",
        "pycurl",
        "qrcode",
        "requests",
        "selenium",
        "setuptools",
        "sh",
        "urllib3",
        "virtualenv",
        "websocket-client",
        "websockets",
        "wheel",
    }
)


@dataclass(frozen=True)
class ScreeningVerdict:
    """Whether code may be run, and if not, every reason found (with its line)."""

    reasons: tuple[str, ...]

    @property
    def accepted(self) -> bool:
        return not self.reasons


@dataclass(frozen=True)
class Requirement:
    """One extra package: a normalized distribution name and, when asked, an exact version."""

    name: str
    version: str | None = None

    def pip_argument(self) -> str:
        return self.name if self.version is None else f"{self.name}=={self.version}"


@dataclass(frozen=True)
class RequirementsVerdict:
    """The packages a run may install, or every reason they may not."""

    requirements: tuple[Requirement, ...]
    reasons: tuple[str, ...]

    @property
    def accepted(self) -> bool:
        return not self.reasons


def normalized_name(name: str) -> str:
    """A distribution's name as PEP 503 compares it."""
    return _NAME_SEPARATORS.sub("-", name).lower()


def screen_requirements(
    requested: Sequence[str], *, max_count: int = MAX_REQUIREMENTS
) -> RequirementsVerdict:
    """Read the packages the model asks to install; refuse anything but a plain name."""
    reasons: list[str] = []
    accepted: list[Requirement] = []
    if len(requested) > max_count:
        reasons.append(f"at most {max_count} packages may be installed, {len(requested)} asked")
    seen: set[str] = set()
    for position, text in enumerate(requested, start=1):
        match = _REQUIREMENT.fullmatch(text)
        if match is None:
            reasons.append(
                f"package {position} is not a name with at most '==version' "
                "(no URL, path, option, extra or range)"
            )
            continue
        name = normalized_name(match["name"])
        if name in DENIED_REQUIREMENTS:
            reasons.append(f"package {position} ({name}) may not be installed")
        elif name in seen:
            reasons.append(f"package {position} ({name}) is asked twice")
        else:
            accepted.append(Requirement(name, match["version"]))
        seen.add(name)
    return RequirementsVerdict(tuple(accepted), tuple(reasons))


def importable_modules(installed: Iterable[str] = ()) -> frozenset[str]:
    """Every top-level module code may import, given the modules installed for its run."""
    return (PREINSTALLED_MODULES | SAFE_STDLIB_MODULES | frozenset(installed)) - FORBIDDEN_MODULES


def screen_code(
    source: str,
    *,
    installed_modules: Iterable[str] = (),
    unlisted_imports_allowed: bool = False,
    max_source_bytes: int = MAX_SOURCE_BYTES,
) -> ScreeningVerdict:
    """Read model-written Python and say whether it may be run in the sandbox.

    ``unlisted_imports_allowed`` is for a first reading before a run's packages are installed,
    when the modules they bring are not known yet: an import outside the list passes, a
    forbidden one does not. The reading after the install leaves it out.
    """
    size = len(source.encode("utf-8"))
    if size > max_source_bytes:
        return ScreeningVerdict((f"the code is {size} bytes, more than {max_source_bytes}",))
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return ScreeningVerdict((f"line {e.lineno or 0}: the code is not valid Python",))
    reader = _Reader(importable_modules(installed_modules), unlisted_imports_allowed)
    reader.visit(tree)
    return ScreeningVerdict(tuple(dict.fromkeys(reader.reasons)))


@dataclass
class _Reader(ast.NodeVisitor):
    allowed_modules: frozenset[str]
    unlisted_imports_allowed: bool
    reasons: list[str] = field(default_factory=list)

    def refuse(self, node: ast.AST, reason: str) -> None:
        self.reasons.append(f"line {getattr(node, 'lineno', 0)}: {reason}")

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.check_module(node, alias.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level or node.module is None:
            self.refuse(node, "relative imports are not allowed")
            return
        self.check_module(node, node.module)
        if node.module.split(".")[0] == "os":
            for alias in node.names:
                if alias.name in FORBIDDEN_OS_FUNCTIONS:
                    self.refuse(node, f"os.{alias.name} is forbidden")
        self.generic_visit(node)

    def check_module(self, node: ast.AST, dotted: str) -> None:
        top = dotted.split(".")[0]
        if top in FORBIDDEN_MODULES:
            self.refuse(node, f"importing {top} is forbidden")
        elif top not in self.allowed_modules and not self.unlisted_imports_allowed:
            self.refuse(node, f"importing {top} is not allowed (not in the sandbox's libraries)")

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in FORBIDDEN_BUILTINS:
            self.refuse(node, f"{node.id} is forbidden")
        elif node.id in FORBIDDEN_NAMES:
            self.refuse(node, f"{node.id} is forbidden")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if _is_refused_attribute(node.attr):
            self.refuse(node, f"the attribute {node.attr} is forbidden")
        if (
            isinstance(node.value, ast.Name)
            and node.value.id == "os"
            and node.attr in FORBIDDEN_OS_FUNCTIONS
        ):
            self.refuse(node, f"os.{node.attr} is forbidden")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        called = _called_name(node.func)
        if called in _ATTRIBUTE_ACCESS_CALLS:
            self.check_attribute_call(node, called)
        if called in _PATH_CALLS and node.args:
            self.check_path(node, node.args[0])
        self.generic_visit(node)

    def check_attribute_call(self, node: ast.Call, called: str) -> None:
        if len(node.args) < 2:
            return
        name = node.args[1]
        if not (isinstance(name, ast.Constant) and isinstance(name.value, str)):
            self.refuse(node, f"{called} with a name computed at run time is forbidden")
        elif _is_refused_attribute(name.value):
            self.refuse(node, f"the attribute {name.value} is forbidden")

    def check_path(self, node: ast.Call, argument: ast.expr) -> None:
        if not (isinstance(argument, ast.Constant) and isinstance(argument.value, str)):
            return
        if not _is_allowed_path(argument.value):
            self.refuse(node, "a file outside /in and /out may not be opened")


def _called_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_refused_attribute(name: str) -> bool:
    if name in FORBIDDEN_ATTRIBUTES:
        return True
    is_dunder = name.startswith("__") and name.endswith("__") and len(name) > 4
    return is_dunder and name not in ALLOWED_DUNDER_ATTRIBUTES


def _is_allowed_path(path: str) -> bool:
    if ".." in path.replace("\\", "/").split("/"):
        return False
    if not path.startswith("/"):
        return True  # relative to the working directory, which is /out
    return any(path == root or path.startswith(root + "/") for root in ALLOWED_PATH_ROOTS)
