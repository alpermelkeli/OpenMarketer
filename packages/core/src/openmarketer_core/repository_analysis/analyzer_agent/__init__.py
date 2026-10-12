"""Repository analyzer: from a safe repository snapshot to a draft Product Profile.

What a caller names of the analysis is here: its result, its limits and its error, from
``rules.py``. The graph is not: ``graph.py`` loads the graph framework, and the worker's
workflow loads this package for the agent's declarations (``definitions.py``), so whoever runs
the analysis imports ``graph.analyze`` itself.
"""

from openmarketer_core.repository_analysis.analyzer_agent.rules import (
    Analysis,
    AnalysisError,
    Limits,
)

__all__ = ["Analysis", "AnalysisError", "Limits"]
