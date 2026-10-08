"""Read-only repository tools for the analyzer agent.

Every tool goes through ``RepoFiles``, so the agent can list, search and read
exactly what intake allows and nothing else. Tool results are plain text and
bounded in size; problems are reported as text so the model can adjust.
"""

from __future__ import annotations

import re
from fnmatch import fnmatch
from typing import Any

from openmarketer_core.intake import ExcludedFileError, RepoFiles

MAX_LISTED = 200
MAX_READ_LINES = 250
MAX_LINE_CHARS = 400
MAX_MATCHES = 40
MAX_SEARCH_BYTES = 512 * 1024

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": (
                "List repository files, optionally filtered by a glob such as "
                "'composeApp/**/*.kt' or '*.md'. Returns at most 200 paths."
            ),
            "parameters": {
                "type": "object",
                "properties": {"glob": {"type": "string"}},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": (
                "Search file contents with a regular expression (case-insensitive). "
                "Returns 'path:line: text' for up to 40 matches. Use 'glob' to narrow the files."
            ),
            "parameters": {
                "type": "object",
                "properties": {"pattern": {"type": "string"}, "glob": {"type": "string"}},
                "required": ["pattern"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read a file with line numbers, at most 250 lines per call. "
                "Use 'start_line' to continue further down."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer", "minimum": 1},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
]


def _matches(path: str, glob: str | None) -> bool:
    if not glob:
        return True
    return (
        fnmatch(path, glob)
        or fnmatch(path, glob.replace("**/", ""))
        or fnmatch(path.rsplit("/", 1)[-1], glob)
    )


class RepoTools:
    def __init__(self, files: RepoFiles) -> None:
        self.files = files
        self._paths = list(files)

    def call(self, name: str, arguments: dict[str, Any]) -> str:
        """Run a tool by name. Never raises for bad input from the model."""
        handler = {
            "list_files": self.list_files,
            "search": self.search,
            "read_file": self.read_file,
        }
        if name not in handler:
            return f"error: unknown tool '{name}'"
        try:
            return handler[name](**arguments)
        except TypeError as e:
            return f"error: bad arguments for {name}: {e}"

    def list_files(self, glob: str | None = None) -> str:
        paths = [p for p in self._paths if _matches(p, glob)]
        if not paths:
            return "no files match"
        shown = paths[:MAX_LISTED]
        more = (
            f"\n... {len(paths) - len(shown)} more; narrow the glob"
            if len(paths) > len(shown)
            else ""
        )
        return "\n".join(shown) + more

    def search(self, pattern: str, glob: str | None = None) -> str:
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            return f"error: invalid regular expression: {e}"
        hits: list[str] = []
        for path in self._paths:
            if not _matches(path, glob):
                continue
            data = self.files.read_bytes(path)
            if len(data) > MAX_SEARCH_BYTES or b"\x00" in data[:2048]:
                continue  # large or binary
            for number, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
                if regex.search(line):
                    hits.append(f"{path}:{number}: {line.strip()[:MAX_LINE_CHARS]}")
                    if len(hits) == MAX_MATCHES:
                        return "\n".join(hits) + "\n... more matches; narrow the search"
        return "\n".join(hits) if hits else "no matches"

    def read_file(self, path: str, start_line: int = 1) -> str:
        try:
            data = self.files.read_bytes(path)
        except ExcludedFileError as e:
            return f"error: {e}"
        except (FileNotFoundError, IsADirectoryError):
            return f"error: no such file: {path}"
        if b"\x00" in data[:2048]:
            return f"error: {path} is a binary file"
        lines = data.decode("utf-8", errors="replace").splitlines()
        start = max(1, start_line)
        chunk = lines[start - 1 : start - 1 + MAX_READ_LINES]
        if not chunk:
            return f"error: {path} has {len(lines)} lines"
        body = "\n".join(
            f"{number}: {line[:MAX_LINE_CHARS]}" for number, line in enumerate(chunk, start)
        )
        end = start + len(chunk) - 1
        if end < len(lines):
            body += f"\n... {len(lines) - end} more lines; continue with start_line={end + 1}"
        return body

    def line_count(self, path: str) -> int | None:
        """Number of lines of a readable file, ``None`` if it cannot be read."""
        try:
            return len(self.files.read_text(path).splitlines())
        except (ExcludedFileError, FileNotFoundError, IsADirectoryError):
            return None
