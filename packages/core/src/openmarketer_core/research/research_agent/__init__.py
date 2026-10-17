"""Researcher: from the request's questions to a report of sourced findings.

What a caller names of a run is here: its result and its error, from ``rules.py``. The graph
is not: ``graph.py`` loads the graph framework, and the worker's workflow loads this package
for the agent's declarations (``definitions.py``), so whoever runs the graph imports
``graph`` itself.
"""

from openmarketer_core.research.research_agent.rules import (
    CheckedFinding,
    Limits,
    Report,
    ResearchError,
)

__all__ = ["CheckedFinding", "Limits", "Report", "ResearchError"]
