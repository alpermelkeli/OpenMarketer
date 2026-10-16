"""The analyzer's read tools: what the model may read of the repository.

The tools are declared on the agent here; importing this module is what puts them on it. What
the model is told of each comes from the function itself (its docstring and type hints,
``agent_runtime.tools.Toolbox``), and each answers with bounded text from the reading of the
attempt's commit (``AnalyzerContext.reading``), never with what the model sent: a problem is
text the model can correct from.

No tool writes anything, asks for an action or decides which files may be read: the reading
does (``reading.py``), over what intake allowed. The submission is not a tool of this module:
``rules.run_tool_calls`` reads it, so the model never has a submission answered as a tool.
"""

from __future__ import annotations

from openmarketer_core.repository_analysis.analyzer_agent.definitions import (
    AnalyzerContext,
    agent,
)


@agent.tool
def list_files(context: AnalyzerContext, glob: str | None = None) -> str:
    """List repository files, optionally filtered by a glob such as
    'composeApp/**/*.kt' or '*.md'. Returns at most 200 paths."""
    return context.reading.list_files(glob)


@agent.tool
def search(context: AnalyzerContext, pattern: str, glob: str | None = None) -> str:
    """Search file contents with a regular expression (case-insensitive).
    Returns 'path:line: text' for up to 40 matches. Use 'glob' to narrow the files."""
    return context.reading.search(pattern, glob)


@agent.tool
def read_file(context: AnalyzerContext, path: str, start_line: int = 1) -> str:
    """Read a file with line numbers, at most 250 lines per call.
    Use 'start_line' to continue further down."""
    return context.reading.read_file(path, start_line)
