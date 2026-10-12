"""How an analysis that produced no profile is told to whoever asked for it.

The errors of an analysis name the folder the repository was cloned into, and
where a service keeps its temporary files is not the caller's to know.
``failure_message`` words such an error for a run's reader; ``ANALYSIS_FAILURES``
is what an analysis raises when it ends without a profile, and anything else is
a defect. This module runs nothing: the worker and the benchmark, which each
take the steps of an analysis their own way, both word a failure with it.
"""

from __future__ import annotations

from pathlib import Path

from openmarketer_core.llm import LLMError
from openmarketer_core.llm_config import ConfigError
from openmarketer_core.repository_analysis.analyzer_agent import AnalysisError
from openmarketer_core.repository_analysis.intake import IntakeError

# What a run that produced no profile raises; anything else is a defect.
ANALYSIS_FAILURES = (IntakeError, AnalysisError, LLMError, ConfigError)
CLONE_PLACEHOLDER = "<clone>"


def failure_message(failure: Exception, workdir: Path) -> str:
    """Why an analysis failed, worded for the person who asked for it.

    git names the folder it clones into, so every path under ``workdir`` (the
    folder the clone was made in) is replaced.
    """
    message = str(failure)
    for path in (str(workdir.resolve()), str(workdir)):
        message = message.replace(path, CLONE_PLACEHOLDER)
    return message
