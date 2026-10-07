"""Repository analyzer: from a safe repository snapshot to a draft Product Profile."""

from openmarketer_core.analyzer.graph import analyze
from openmarketer_core.analyzer.rules import Analysis, AnalysisError, Limits

__all__ = ["Analysis", "AnalysisError", "Limits", "analyze"]
