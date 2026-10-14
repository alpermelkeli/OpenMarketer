"""What the runtime's activities do for a run of an analysis: the worker's side of its graph.

The analyzer's graph and its rules are core's. ``AnalysisWork`` is what stands between them
and this worker's database, disk and model, one method for each thing the agent runtime asks
an agent for:

- ``initial_state``: what a run starts from, which is the run's own identifiers;
- ``context``: what the nodes of one attempt are given: a chat model of the attempt's own and
  the limits, both made for the run's workspace, and the run's repository through
  ``LocalClones``;
- ``clone``: the handler of the action ``clone_repository``;
- ``finish``: storing the profile and marking the run succeeded, both or neither;
- ``release``: removing the run's clone once the run is over.

``WordedSteps`` wraps the steps of the graph so that what an analysis fails with leaves the
activity as a failure with a type and a message that may be stored and shown.

They hold no rule of their own: which repository may be cloned, what a profile must hold,
which value a credential has and how a run changes state are core's. What they add is what
an activity owes the engine. A failure says whether another attempt is worth its cost, and
none carries the clone's folder or its original error, which is logged. Everything can run
twice without doing its work twice: a clone is the same clone, and a profile is stored once.

A step keeps the run's checkpoints on a failure that will be tried again. They are forgotten
by the runtime when the run ends; here only by an attempt that finds, when it is done, that
its run was recorded failed meanwhile, for what it wrote since. Dependencies are handed in
when the worker starts; nothing here reads the environment.

No credential is kept here or put in the graph's state: the model of an attempt, the tokens
of a clone and the limits of an attempt are asked for with the run's workspace
(``new_model``, ``tokens_of``, ``limits_of``), each time one is needed, so a key or a model
set from the dashboard is the one the next attempt uses. A key that is set nowhere, or a
credential or setting that is stored and cannot be read (``WORKSPACE_FAILURES``), ends the
analysis in the words of its error, which name what to set and hold no value.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from openmarketer_core.agent_runtime.actions import ActionOutcome
from openmarketer_core.agent_runtime.steps import GraphRunRef, StepOutcome
from openmarketer_core.credentials.resolution import CredentialUnreadable
from openmarketer_core.db.analysis_runs import (
    AnalysisRunFinished,
    AnalysisRunNotFound,
    analysis_run,
    mark_run_succeeded,
    run_thread_id,
    run_to_execute,
)
from openmarketer_core.db.evidence_store import save_analysis_of_project
from openmarketer_core.db.models import AnalysisRunStatus
from openmarketer_core.db.projects import ProjectNotFound, get_project
from openmarketer_core.db.session import DatabaseError, transaction
from openmarketer_core.graph_checkpoints import RunCheckpoints
from openmarketer_core.llm import ChatModel, LLMError
from openmarketer_core.repository_analysis.analyzer_agent import Limits
from openmarketer_core.repository_analysis.analyzer_agent.definitions import (
    AnalyzerContext,
    AnalyzerState,
    CloneRepository,
    analysis_of,
    initial_state,
)
from openmarketer_core.repository_analysis.extraction import (
    ExtractedFact,
    Extractor,
    run_extractors,
)
from openmarketer_core.repository_analysis.failures import ANALYSIS_FAILURES, failure_message
from openmarketer_core.repository_analysis.intake import (
    IntakeError,
    RepoFiles,
    RepositoryTokens,
    remote_repository_url,
)
from openmarketer_core.repository_analysis.workflow_contract import AnalyzeRepositoryInput
from openmarketer_core.workspace_settings.analysis_settings import StoredAnalysisLimitsInvalid
from openmarketer_core.workspace_settings.model_settings import StoredModelChoiceInvalid
from openmarketer_worker.agent_runtime.activities import AgentGraph
from openmarketer_worker.agent_runtime.activity_support import CheckpointAccess, forget_quietly
from openmarketer_worker.agent_runtime.vocabulary import DATABASE_UNAVAILABLE
from openmarketer_worker.credentials import ModelKeyNotSet
from openmarketer_worker.repository_analyzer import steps
from openmarketer_worker.repository_analyzer.activities import failure, scope_of, subject_of
from openmarketer_worker.repository_analyzer.clones import LocalClones, TokensOfTheRun

logger = logging.getLogger(__name__)

# What the credentials and settings of a run's workspace are refused with: the model
# provider's key is set nowhere, a stored value does not open, or a stored choice breaks a
# rule. None passes by itself, and each is worded for whoever asked for the analysis.
WORKSPACE_FAILURES = (
    ModelKeyNotSet,
    CredentialUnreadable,
    StoredModelChoiceInvalid,
    StoredAnalysisLimitsInvalid,
)


class AnalysisWork:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        checkpoints: CheckpointAccess,
        clones: LocalClones,
        *,
        new_model: Callable[[uuid.UUID], ChatModel],
        tokens_of: Callable[[uuid.UUID], RepositoryTokens],
        extractors: Sequence[Extractor],
        limits_of: Callable[[uuid.UUID], Limits],
    ) -> None:
        """``new_model`` makes the chat model of one attempt: a model keeps what it served.

        It is given the workspace of the run, as ``tokens_of`` is, which gives the tokens a
        clone for a run of that workspace may carry, and ``limits_of``, which gives the
        limits of an attempt. Each may raise one of ``WORKSPACE_FAILURES``, which ends the
        analysis.
        """
        self._sessions = sessions
        self._checkpoints = checkpoints
        self._clones = clones
        self._new_model = new_model
        self._tokens_of = tokens_of
        self._extractors = extractors
        self._limits_of = limits_of

    def initial_state(self, run: GraphRunRef) -> AnalyzerState:
        """What the graph of ``run`` starts from: the run with its project and workspace."""
        return initial_state(self._run_to_execute(run))

    def context(self, run: GraphRunRef, checkpoints: RunCheckpoints) -> AnalyzerContext:
        """What the nodes of one attempt at a step of ``run`` are given."""

        def files_at(commit_sha: str) -> RepoFiles:
            found = self._run_to_execute(run)
            source = self._repository_of(found)
            return self._clones.at_commit(
                run.run_id, source, commit_sha, self._tokens_for(found)
            ).files

        workspace_id = self._run_to_execute(run).workspace_id
        return AnalyzerContext(
            model=self._new_model(workspace_id),
            limits=self._limits_of(workspace_id),
            files_at=files_at,
            facts_of=self._facts_of,
        )

    def clone(self, payload: CloneRepository) -> Mapping[str, Any]:
        """Clone the repository of the run's project onto this worker; the commit it is at.

        A repository that is refused is not tried again, and neither is a token that cannot
        be read; the graph ends the analysis with what this says of it.
        """
        run = AnalyzeRepositoryInput(
            workspace_id=payload.workspace_id,
            project_id=payload.project_id,
            run_id=payload.run_id,
        )
        source = self._repository_of(run)
        try:
            intake = self._clones.clone(run.run_id, source, self._tokens_for(run))
        except (IntakeError, *WORKSPACE_FAILURES) as e:
            logger.info("analysis run %s: its repository was not cloned: %s", run.run_id, e)
            raise failure(
                steps.ANALYSIS_FAILED, failure_message(e, self._clones.folder_of(run.run_id))
            ) from None
        return {"commit_sha": intake.snapshot.commit_sha}

    def finish(self, run: GraphRunRef, final_state: Mapping[str, Any]) -> dict[str, Any]:
        """Store the profile the graph accepted as the run's result; the id of what was stored.

        Safe to repeat: a run that has its result is given no second one.
        """
        finished = AnalyzeRepositoryInput(
            workspace_id=uuid.UUID(final_state["workspace_id"]),
            project_id=uuid.UUID(final_state["project_id"]),
            run_id=uuid.UUID(final_state["run_id"]),
        )
        stored = self._result_of(finished)
        if stored is None:
            stored = self._store(finished, final_state)
        return {"profile_id": str(stored)}

    def release(self, run: GraphRunRef) -> None:
        """Remove what the run left on this worker's disk."""
        self._clones.release(run.run_id)

    def _run_to_execute(self, run: GraphRunRef) -> AnalyzeRepositoryInput:
        with transaction(self._sessions) as session:
            found = run_to_execute(session, run.run_id)
        if found is None:
            raise failure(steps.RUN_UNAVAILABLE, f"there is no analysis run {run.run_id}")
        return found

    def _tokens_for(self, run: AnalyzeRepositoryInput) -> TokensOfTheRun:
        """How a clone of ``run`` gets its tokens: those of the run's workspace, when it asks."""
        return lambda: self._tokens_of(run.workspace_id)

    def _facts_of(self, files: RepoFiles) -> list[ExtractedFact]:
        return run_extractors(files, self._extractors).facts

    def _repository_of(self, run: AnalyzeRepositoryInput) -> str:
        """The URL to clone for a run that is still to be analysed."""
        try:
            with transaction(self._sessions) as session:
                stored_run = analysis_run(session, **scope_of(run))
                if stored_run.status in (AnalysisRunStatus.SUCCEEDED, AnalysisRunStatus.FAILED):
                    raise AnalysisRunFinished(run.run_id, stored_run.status)
                project = get_project(
                    session, workspace_id=run.workspace_id, project_id=run.project_id
                )
            # The worker clones only what a remote caller may name, never a local folder.
            return remote_repository_url(project.source_repo_url)
        except (AnalysisRunNotFound, AnalysisRunFinished, ProjectNotFound) as e:
            raise failure(steps.RUN_UNAVAILABLE, str(e)) from None
        except IntakeError as e:
            raise failure(steps.ANALYSIS_FAILED, str(e)) from None

    def _result_of(self, run: AnalyzeRepositoryInput) -> uuid.UUID | None:
        """The profile version the run succeeded with, or ``None`` while it has no outcome.

        A run that was recorded failed takes no result: the attempt is refused for good.
        """
        try:
            with transaction(self._sessions) as session:
                stored_run = analysis_run(session, **scope_of(run))
        except AnalysisRunNotFound as e:
            raise failure(steps.RUN_UNAVAILABLE, str(e)) from None
        if stored_run.status is AnalysisRunStatus.FAILED:
            # Its checkpoints were forgotten and its clone removed when the failure was
            # recorded; this removes what the attempt has written since.
            forget_quietly(
                self._checkpoints, run_thread_id(run.run_id), subject=subject_of(run.run_id)
            )
            self._clones.release(run.run_id)
            refused = AnalysisRunFinished(run.run_id, stored_run.status)
            raise failure(steps.RUN_UNAVAILABLE, str(refused))
        return stored_run.profile_id

    def _store(self, run: AnalyzeRepositoryInput, final_state: Mapping[str, Any]) -> uuid.UUID:
        """Store the result and mark the run succeeded, both or neither."""
        analysis = analysis_of(final_state)
        intake = self._clones.at_commit(
            run.run_id, self._repository_of(run), final_state["commit_sha"], self._tokens_for(run)
        )
        try:
            with transaction(self._sessions) as session:
                saved = save_analysis_of_project(
                    session,
                    workspace_id=run.workspace_id,
                    project_id=run.project_id,
                    snapshot=intake.snapshot,
                    # Found again, and the same: the extractors are deterministic, and what
                    # they found is not kept in the graph's state.
                    facts=self._facts_of(intake.files),
                    profile=analysis.profile,
                )
                mark_run_succeeded(session, **scope_of(run), profile_id=saved.profile_id)
        except AnalysisRunFinished as e:
            # Another attempt finished the run first; what this one stored was rolled back.
            stored = self._result_of(run)
            if stored is None:
                raise failure(steps.RUN_UNAVAILABLE, str(e)) from None
            return stored
        except (AnalysisRunNotFound, ProjectNotFound) as e:
            raise failure(steps.RUN_UNAVAILABLE, str(e)) from None
        except DatabaseError:
            logger.exception("the profile of analysis run %s was not stored", run.run_id)
            raise failure(DATABASE_UNAVAILABLE, steps.NOT_STORED) from None
        return saved.profile_id


class WordedSteps:
    """The steps of the analyzer's graph, with an analysis's failures worded for its run."""

    def __init__(self, graph: AgentGraph, clones: LocalClones) -> None:
        self._graph = graph
        self._clones = clones

    def advance(
        self, run: GraphRunRef, checkpoints: RunCheckpoints, resume: ActionOutcome | None
    ) -> StepOutcome:
        """Take the step, as ``AgentGraph.advance`` says.

        A refused repository or profile ends the analysis (``ANALYSIS_FAILED``), as does a
        credential that is not set or cannot be read; only the model provider fails in ways
        that pass, an outage or a rate limit (``MODEL_UNAVAILABLE``, tried again). The message
        never names the clone's folder.
        """
        try:
            return self._graph.advance(run, checkpoints, resume)
        except (*ANALYSIS_FAILURES, *WORKSPACE_FAILURES) as e:
            logger.info("analysis run %s failed: %s", run.run_id, e)
            passing = isinstance(e, LLMError) and e.may_pass
            kind = steps.MODEL_UNAVAILABLE if passing else steps.ANALYSIS_FAILED
            message = failure_message(e, self._clones.folder_of(run.run_id))
            raise failure(kind, message) from None
