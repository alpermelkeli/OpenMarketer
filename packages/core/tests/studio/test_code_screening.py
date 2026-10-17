"""Specifications of the first look at model-written Python, before the sandbox runs it."""

from openmarketer_core.studio.code_screening import MAX_SOURCE_BYTES, screen_code

COMPOSITION = """
import json
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

brand = json.loads(Path("/in/brand_kit.json").read_text())
canvas = Image.new("RGB", (1080, 1080), brand["primary"])
font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 64)
ImageDraw.Draw(canvas).text((math.floor(40.5), 40), "Hello", font=font)
canvas.save("/out/post.png")
"""


def reasons(source: str) -> tuple[str, ...]:
    return screen_code(source).reasons


def test_a_composition_with_the_image_libraries_is_accepted() -> None:
    assert screen_code(COMPOSITION).accepted


def test_importing_subprocess_is_refused() -> None:
    assert reasons("import subprocess") == ("line 1: importing subprocess is forbidden",)


def test_importing_a_submodule_of_a_forbidden_module_is_refused() -> None:
    assert reasons("from urllib.request import urlopen") == (
        "line 1: importing urllib is forbidden",
    )


def test_importing_socket_is_refused() -> None:
    assert not screen_code("import socket").accepted


def test_importing_ctypes_is_refused() -> None:
    assert not screen_code("import ctypes").accepted


def test_importing_importlib_is_refused() -> None:
    assert not screen_code("import importlib").accepted


def test_a_module_outside_the_list_is_refused() -> None:
    assert reasons("import matplotlib") == (
        "line 1: importing matplotlib is not allowed (not in the sandbox's libraries)",
    )


def test_a_module_installed_for_the_run_is_accepted() -> None:
    assert screen_code("import skimage", installed_modules=["skimage"]).accepted


def test_an_installed_module_cannot_make_a_forbidden_one_importable() -> None:
    assert not screen_code("import socket", installed_modules=["socket"]).accepted


def test_a_first_look_lets_an_unlisted_import_pass() -> None:
    assert screen_code("import skimage", unlisted_imports_allowed=True).accepted


def test_a_first_look_still_refuses_a_forbidden_import() -> None:
    assert not screen_code("import subprocess", unlisted_imports_allowed=True).accepted


def test_a_relative_import_is_refused() -> None:
    assert reasons("from . import helpers") == ("line 1: relative imports are not allowed",)


def test_os_system_is_refused() -> None:
    assert reasons("import os\nos.system('ls')") == ("line 2: os.system is forbidden",)


def test_os_popen_imported_by_name_is_refused() -> None:
    assert reasons("from os import popen") == ("line 1: os.popen is forbidden",)


def test_os_exec_functions_are_refused() -> None:
    assert not screen_code("import os\nos.execv('/bin/sh', [])").accepted


def test_os_fork_is_refused() -> None:
    assert not screen_code("import os\nos.fork()").accepted


def test_os_path_is_accepted() -> None:
    assert screen_code("import os\nprint(os.path.join('/out', 'a.png'))").accepted


def test_eval_is_refused() -> None:
    assert reasons("eval('1 + 1')") == ("line 1: eval is forbidden",)


def test_exec_is_refused() -> None:
    assert not screen_code("exec('x = 1')").accepted


def test_compile_is_refused() -> None:
    assert not screen_code("compile('x', 'f', 'exec')").accepted


def test_dunder_import_is_refused() -> None:
    assert not screen_code("__import__('os')").accepted


def test_walking_to_subclasses_is_refused() -> None:
    assert "line 1: the attribute __subclasses__ is forbidden" in reasons(
        "().__class__.__bases__[0].__subclasses__()"
    )


def test_reaching_a_function_s_globals_is_refused() -> None:
    assert reasons("def f(): pass\nf.__globals__") == (
        "line 2: the attribute __globals__ is forbidden",
    )


def test_naming_builtins_is_refused() -> None:
    assert reasons("__builtins__") == ("line 1: __builtins__ is forbidden",)


def test_getattr_of_a_dunder_name_is_refused() -> None:
    assert reasons("getattr(object, '__subclasses__')") == (
        "line 1: the attribute __subclasses__ is forbidden",
    )


def test_getattr_of_a_name_computed_at_run_time_is_refused() -> None:
    assert reasons("name = 'x'\ngetattr(object, name)") == (
        "line 2: getattr with a name computed at run time is forbidden",
    )


def test_a_frame_s_globals_are_refused() -> None:
    assert not screen_code("def g():\n    yield\ng().gi_frame.f_globals").accepted


def test_super_init_is_accepted() -> None:
    source = "class A:\n    def __init__(self):\n        super().__init__()\n"
    assert screen_code(source).accepted


def test_opening_a_file_outside_in_and_out_is_refused() -> None:
    assert reasons("open('/etc/passwd')") == (
        "line 1: a file outside /in and /out may not be opened",
    )


def test_a_path_outside_in_and_out_is_refused() -> None:
    assert not screen_code("from pathlib import Path\nPath('/proc/self/environ')").accepted


def test_a_path_that_climbs_out_of_out_is_refused() -> None:
    assert not screen_code("open('/out/../etc/passwd')").accepted


def test_opening_an_input_is_accepted() -> None:
    assert screen_code("open('/in/brand_kit.json')").accepted


def test_a_relative_path_is_accepted() -> None:
    assert screen_code("open('result.png', 'wb')").accepted


def test_a_path_computed_at_run_time_is_left_to_the_container() -> None:
    assert screen_code("import os\nopen(os.path.join('/', 'etc', 'passwd'))").accepted


def test_code_over_the_size_limit_is_refused() -> None:
    source = "x = 1\n" * (MAX_SOURCE_BYTES // 6 + 1)
    assert reasons(source) == (f"the code is {len(source)} bytes, more than {MAX_SOURCE_BYTES}",)


def test_code_that_does_not_parse_is_refused() -> None:
    assert reasons("def broken(:\n") == ("line 1: the code is not valid Python",)


def test_every_reason_is_given() -> None:
    assert len(reasons("import socket\neval('1')")) == 2
