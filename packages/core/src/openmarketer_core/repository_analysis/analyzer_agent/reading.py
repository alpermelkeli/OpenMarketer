"""The analyzer's reading: the bounded, read-only view over a repository snapshot.

``RepoTools`` is what answers the model's calls: list, search, read and a line count for the
evidence checks in ``rules.py``. Every answer is plain text, bounded in size, and problems are
reported as text so the model can correct from them; nothing here raises for what the model
sent. The reading goes through ``RepoFiles``, so it reaches exactly what intake allows and
nothing else.

The tools the model may call are declared on the agent in ``tools.py``; this module knows no
agent, no model, no framework and no database.
"""

from __future__ import annotations

import re
from fnmatch import fnmatch

from openmarketer_core.repository_analysis.intake import ExcludedFileError, RepoFiles

MAX_LISTED = 200
MAX_READ_LINES = 250
MAX_LINE_CHARS = 400
MAX_MATCHES = 40
MAX_SEARCH_BYTES = 512 * 1024


def _matches(path: str, glob: str | None) -> bool:
    if not glob:
        return True
    return (
        fnmatch(path, glob)
        or fnmatch(path, glob.replace("**/", ""))
        or fnmatch(path.rsplit("/", 1)[-1], glob)
    )


class RepoTools:
    """The read-only tools over the files of one repository snapshot."""

    def __init__(self, files: RepoFiles) -> None:
        self.files = files
        self._paths = list(files)

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
