"""The analyzer's declarations: the agent, its state, what its nodes are given, and its action.

This is the module the worker's workflow loads, so it imports no graph framework and nothing
that touches the database. The nodes are in ``graph.py``, which only the process that runs the
graph imports; importing that module is what puts them on ``agent``.

- ``AnalyzerState`` is what the graph keeps between steps and what a checkpoint stores: plain
  data only. It names the run and the commit the run reads, so a later attempt reads the same
  commit whatever the repository's branch has become since.
- ``AnalyzerContext`` is what a node needs that is not state: the model, the limits and the
  repository. It is built for each attempt by whoever runs the graph and is never stored.
- ``CloneRepository`` is the one action the graph asks for. Cloning reaches a host on the
  network and writes a disk, so it is the workflow's to run, under limits of its own.

The payload of the action names the run and nothing else. The workflow's history keeps every
payload, so no repository URL, token, path or file content is ever in one
(``workflow_contract.py``): whoever runs the action reads the repository of the run's project
from the database.

Nothing here runs: no node, no model call, no clone. The rules of the analysis are in
``rules.py``.
"""

import operator
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any, TypedDict

from pydantic import BaseModel, ConfigDict

from openmarketer_core.agent_runtime.agent import Agent
from openmarketer_core.llm import ChatModel
from openmarketer_core.profile import ProductProfile
from openmarketer_core.repository_analysis.analyzer_agent.rules import Analysis, Limits
from openmarketer_core.repository_analysis.analyzer_agent.tools import RepoTools
from openmarketer_core.repository_analysis.extraction import ExtractedFact
from openmarketer_core.repository_analysis.intake import RepoFiles
from openmarketer_core.repository_analysis.workflow_contract import (
    ANALYSIS_RUN_KIND,
    AnalyzeRepositoryInput,
)

CLONE_REPOSITORY = "clone_repository"

# How the clone is tried. These are constants of the action and not settings: an operator sets
# how long the model's work may take (``ANALYSIS_TIMEOUT_MINUTES``) and how often it is tried
# (``ANALYSIS_MAX_ATTEMPTS``), and neither says anything of a clone.
# - git is stopped after five minutes and each pass of the secret scan after five more
#   (``intake/git.py``, ``intake/secrets.py``), so an attempt that is still running after
#   fifteen is hung.
# - A repository that is refused is never tried again: whoever runs the action says so. What
#   is tried again is a worker that died and a database that could not be reached, and five
#   attempts that wait 1, 2, 4 and 8 minutes ride out a quarter of an hour of either, as the
#   attempts at the model's work do. A clone is safe to repeat: it writes a folder of the run.
# - A minute without a heartbeat is a worker that is gone.
CLONE_TIMEOUT_SECONDS = 15 * 60
CLONE_ATTEMPTS = 5
CLONE_RETRY_AFTER_SECONDS = 60
CLONE_HEARTBEAT_SECONDS = 60


class AnalyzerState(TypedDict):
    # The run, as text because a checkpoint stores plain data; empty in a run with no workflow.
    workspace_id: str
    project_id: str
    run_id: str
    commit_sha: str  # the commit the run reads, once it is cloned; empty until then
    messages: Annotated[list[dict[str, Any]], operator.add]
    tool_calls: Annotated[list[str], operator.add]
    pending_calls: list[dict[str, Any]]  # tool calls of the latest model turn
    step: int
    cost_usd: float
    submissions: int
    wrapping_up: bool
    wrap_up_sent: bool
    model: str
    profile: dict[str, Any] | None  # the accepted profile, as JSON data
    notes: list[str]


@dataclass
class AnalyzerContext:
    """What the nodes of one attempt are given besides the state.

    ``files_at`` gives the files of the repository at a commit, already scanned and redacted
    by intake, and ``facts_of`` what the extractors found in them. Both may be slow (a clone
    that is no longer on this machine is made again), so the files are asked for once in an
    attempt and kept here. A context serves one attempt, in one thread.
    """

    model: ChatModel
    limits: Limits
    files_at: Callable[[str], RepoFiles]
    facts_of: Callable[[RepoFiles], Iterable[ExtractedFact]]
    _tools: dict[str, RepoTools] = field(default_factory=dict, init=False, repr=False)

    def tools(self, commit_sha: str) -> RepoTools:
        """The read-only tools over the files of ``commit_sha``, the same ones each time."""
        if commit_sha not in self._tools:
            self._tools[commit_sha] = RepoTools(self.files_at(commit_sha))
        return self._tools[commit_sha]


agent = Agent(ANALYSIS_RUN_KIND, state=AnalyzerState, context=AnalyzerContext)


@agent.action(
    CLONE_REPOSITORY,
    timeout=CLONE_TIMEOUT_SECONDS,
    attempts=CLONE_ATTEMPTS,
    retry_after=CLONE_RETRY_AFTER_SECONDS,
    heartbeat=CLONE_HEARTBEAT_SECONDS,
)
class CloneRepository(BaseModel):
    """Clone the repository of the run's project, and say which commit was cloned.

    The answer is ``{"commit_sha": ...}`` and nothing else, for the reason the payload holds
    identifiers only.
    """

    model_config = ConfigDict(extra="forbid")

    workspace_id: uuid.UUID
    project_id: uuid.UUID
    run_id: uuid.UUID


def initial_state(run: AnalyzeRepositoryInput | None = None) -> AnalyzerState:
    """What an analysis starts from: nothing read, nothing spent.

    ``run`` is the run a workflow executes. It is left out by a run with no workflow, whose
    repository its caller has cloned already.
    """
    return {
        "workspace_id": "" if run is None else str(run.workspace_id),
        "project_id": "" if run is None else str(run.project_id),
        "run_id": "" if run is None else str(run.run_id),
        "commit_sha": "",
        "messages": [],
        "tool_calls": [],
        "pending_calls": [],
        "step": 0,
        "cost_usd": 0.0,
        "submissions": 0,
        "wrapping_up": False,
        "wrap_up_sent": False,
        "model": "",
        "profile": None,
        "notes": [],
    }


def analysis_of(final_state: Mapping[str, Any]) -> Analysis:
    """The result of an analysis whose graph finished: the accepted profile and what it cost.

    The steps and the cost count every attempt at the run, because both are in the state.
    """
    return Analysis(
        profile=ProductProfile.model_validate(final_state["profile"]),
        cost_usd=final_state["cost_usd"],
        steps=final_state["step"],
        model=final_state["model"],
        tool_calls=final_state["tool_calls"],
        notes=final_state["notes"],
    )
