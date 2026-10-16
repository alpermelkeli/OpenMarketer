"""What the runtime's activities do for a run of a research: the worker's side of its graph.

The researcher's graph and its rules are core's. ``ResearchWork`` is what stands between them
and this worker's database, object storage, search and rendering, one method for each thing
the agent runtime asks an agent for:

- ``initial_state``: what a run starts from, which is the run's own request;
- ``context``: what the nodes of one attempt are given: a chat model of the attempt's own
  and the stored text of the pages the run fetched;
- ``search``: the handler of the action ``search_web``;
- ``fetch``: the handler of the action ``fetch_page``: it refuses what may not be fetched
  (``page_fetch.py``), has the headless browser render a script shell, stores the page in
  object storage and its row in the database;
- ``finish``: storing the report and marking the run succeeded, both or neither.

``WordedSteps`` wraps the steps of the graph so that what a research fails with leaves the
activity as a failure with a type and a message that may be stored and shown.

They hold no rule of their own: what may be fetched, what a finding must rest on, which
value a credential has and how a run changes state are core's. What they add is what an
activity owes the engine. A failure says whether another attempt is worth its cost, and none
carries a message written for a log. Everything can run twice without doing its work twice:
a search reads and changes nothing, a page of the same URL of the same run is stored once,
and a report is stored once.

No credential is kept here or put in the graph's state: the model of an attempt is asked for
with the run's workspace (``new_model``) each time one is needed, so a key or a model set
from the dashboard is the one the next attempt uses. A key that is set nowhere, or a
credential that is stored and cannot be read (``WORKSPACE_FAILURES``), ends the research in
the words of its error, which name what to set and hold no value.

A page's text is never in the graph's state or the workflow history: it is read from object
storage while a node runs, and what the graph carries is the page's id.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from openmarketer_core.agent_runtime.actions import ActionOutcome
from openmarketer_core.agent_runtime.steps import GraphRunRef, StepOutcome
from openmarketer_core.credentials.resolution import CredentialUnreadable, ModelKeyNotSet
from openmarketer_core.db.models import ResearchRunStatus
from openmarketer_core.db.projects import ProjectNotFound
from openmarketer_core.db.research_pages import (
    NewPage,
    ResearchPage,
    ResearchPageNotFound,
    page_of,
    page_of_url,
    store_page,
)
from openmarketer_core.db.research_runs import (
    ResearchRunFinished,
    ResearchRunNotFound,
    mark_run_succeeded,
    research_run,
    run_thread_id,
    run_to_execute,
)
from openmarketer_core.db.research_store import save_research_report
from openmarketer_core.db.session import DatabaseError, transaction
from openmarketer_core.graph_checkpoints import RunCheckpoints
from openmarketer_core.llm import ChatModel, LLMError
from openmarketer_core.research.failures import RESEARCH_FAILURES
from openmarketer_core.research.page_fetch import FetchRefused, FetchUnavailable, PageFetcher
from openmarketer_core.research.page_store import PageStore, PageStoreUnavailable, page_key
from openmarketer_core.research.page_text import PageTextUnavailable, plain_text, title_of
from openmarketer_core.research.questions import StoredResearchRequestInvalid
from openmarketer_core.research.research_agent import rules
from openmarketer_core.research.research_agent.definitions import (
    FetchPage,
    ResearchContext,
    ResearchState,
    SearchWeb,
    initial_state,
    report_of,
)
from openmarketer_core.research.web_search import MAX_RESULTS, SearchEngine, SearchUnavailable
from openmarketer_core.research.workflow_contract import ResearchRunInput
from openmarketer_core.workspace_settings.model_settings import StoredModelChoiceInvalid
from openmarketer_worker.agent_runtime.activities import AgentGraph
from openmarketer_worker.agent_runtime.activity_support import CheckpointAccess, forget_quietly
from openmarketer_worker.agent_runtime.vocabulary import DATABASE_UNAVAILABLE
from openmarketer_worker.researcher import steps
from openmarketer_worker.researcher.activities import failure, subject_of

logger = logging.getLogger(__name__)

# What the credentials of a run's workspace are refused with: the model provider's key is
# set nowhere, a stored value does not open, or a stored model choice breaks a rule. None
# passes by itself, and each is worded for whoever asked for the research.
WORKSPACE_FAILURES = (ModelKeyNotSet, CredentialUnreadable, StoredModelChoiceInvalid)


class ResearchWork:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        checkpoints: CheckpointAccess,
        *,
        new_model: Callable[[uuid.UUID], ChatModel],
        limits: rules.Limits,
        search: SearchEngine,
        fetcher: PageFetcher,
        pages: PageStore,
    ) -> None:
        """``new_model`` makes the chat model of one attempt: a model keeps what it served.

        It is given the workspace of the run and may raise one of ``WORKSPACE_FAILURES``,
        which ends the research. ``search`` and ``fetcher`` reach the installation's own
        services; ``pages`` keeps what was fetched until it expires.
        """
        self._sessions = sessions
        self._checkpoints = checkpoints
        self._new_model = new_model
        self._limits = limits
        self._search = search
        self._fetcher = fetcher
        self._pages = pages

    def initial_state(self, run: GraphRunRef) -> ResearchState:
        """What the graph of ``run`` starts from: the run and the request it answers."""
        found = self._run_to_execute(run)
        return initial_state(
            ResearchRunInput(
                workspace_id=found.workspace_id,
                project_id=found.project_id,
                run_id=found.run_id,
            ),
            found.request,
        )

    def context(self, run: GraphRunRef, checkpoints: RunCheckpoints) -> ResearchContext:
        """What the nodes of one attempt at a step of ``run`` are given."""
        found = self._run_to_execute(run)

        def page_text_of(page_id: str) -> str:
            return self._page_text(found.workspace_id, found.project_id, page_id)

        return ResearchContext(
            model=self._new_model(found.workspace_id),
            limits=self._limits,
            page_text_of=page_text_of,
        )

    def search(self, payload: SearchWeb) -> Mapping[str, Any]:
        """Search the installation's own instance for the query; the results, bounded.

        A service that does not answer may pass, so the action is tried again; what it
        answers with is data and only its address, title and snippet are kept.
        """
        try:
            results = self._search.search(
                payload.query, categories=payload.categories, limit=MAX_RESULTS
            )
        except SearchUnavailable as e:
            raise failure(steps.RESEARCH_UNAVAILABLE, str(e)) from None
        return {
            "results": [
                {"url": result.url, "title": result.title, "snippet": result.snippet}
                for result in results
            ]
        }

    def fetch(self, payload: FetchPage) -> Mapping[str, Any]:
        """Fetch the page of the run at ``payload.url`` and store it; its id and flags.

        A page whose URL the run already has is answered from its row: a repeated fetch of
        the same page of the same run fetches and stores it once. A page that may not be
        fetched is refused for good; one that cannot be reached now may pass, so the action
        is tried again.
        """
        run = ResearchRunInput(
            workspace_id=payload.workspace_id,
            project_id=payload.project_id,
            run_id=payload.run_id,
        )
        existing = self._page_at(run, payload.url)
        if existing is not None:
            return _page_answer(existing)
        try:
            page = self._fetcher.fetch(payload.url)
        except FetchRefused as e:
            raise failure(steps.RESEARCH_FAILED, str(e)) from None
        except FetchUnavailable as e:
            raise failure(steps.RESEARCH_UNAVAILABLE, str(e)) from None
        text = page.body.decode(errors="replace")
        flags = rules.injection_signs(plain_text(text))
        page_id = uuid.uuid4()
        key = page_key(
            workspace_id=run.workspace_id,
            project_id=run.project_id,
            run_id=run.run_id,
            page_id=page_id,
        )
        try:
            self._pages.put(key, page.body)
        except PageStoreUnavailable as e:
            raise failure(steps.RESEARCH_UNAVAILABLE, str(e)) from None
        try:
            with transaction(self._sessions) as session:
                stored = store_page(
                    session,
                    workspace_id=run.workspace_id,
                    project_id=run.project_id,
                    run_id=run.run_id,
                    page_id=page_id,
                    page=NewPage(
                        url=page.url,
                        title=title_of(text),
                        content_type=page.content_type,
                        size_bytes=len(page.body),
                        storage_key=key,
                        sha256=hashlib.sha256(page.body).hexdigest(),
                        flags=flags,
                    ),
                )
        except (ResearchRunNotFound, ResearchRunFinished, ProjectNotFound) as e:
            self._remove_page_quietly(key)
            raise failure(steps.RUN_UNAVAILABLE, str(e)) from None
        except StoredResearchRequestInvalid as e:
            self._remove_page_quietly(key)
            raise failure(steps.RESEARCH_FAILED, str(e)) from None
        except DatabaseError:
            self._remove_page_quietly(key)
            logger.exception("a page of research run %s was not stored", run.run_id)
            raise failure(DATABASE_UNAVAILABLE, steps.PAGE_NOT_STORED) from None
        return _page_answer(stored)

    def finish(self, run: GraphRunRef, final_state: Mapping[str, Any]) -> dict[str, Any]:
        """Store the report the graph finished with as the run's result; its id.

        Safe to repeat: a run that has its result is given no second one.
        """
        finished = ResearchRunInput(
            workspace_id=uuid.UUID(final_state["workspace_id"]),
            project_id=uuid.UUID(final_state["project_id"]),
            run_id=uuid.UUID(final_state["run_id"]),
        )
        stored = self._result_of(finished)
        if stored is None:
            stored = self._store(finished, final_state)
        return {"report_id": str(stored)}

    def _run_to_execute(self, run: GraphRunRef):
        with transaction(self._sessions) as session:
            found = run_to_execute(session, run.run_id)
        if found is None:
            raise failure(steps.RUN_UNAVAILABLE, f"there is no research run {run.run_id}")
        return found

    def _page_at(self, run: ResearchRunInput, url: str) -> ResearchPage | None:
        with transaction(self._sessions) as session:
            return page_of_url(
                session,
                workspace_id=run.workspace_id,
                project_id=run.project_id,
                run_id=run.run_id,
                url=url,
            )

    def _page_text(self, workspace_id: uuid.UUID, project_id: uuid.UUID, page_id: str) -> str:
        """The stored text of one page of the run, or ``PageTextUnavailable``."""
        try:
            parsed = uuid.UUID(page_id)
        except ValueError:
            raise PageTextUnavailable("the page's identifier is unreadable") from None
        try:
            with transaction(self._sessions) as session:
                page = page_of(
                    session, workspace_id=workspace_id, project_id=project_id, page_id=parsed
                )
        except ResearchPageNotFound:
            raise PageTextUnavailable("the page is not stored any more") from None
        except DatabaseError:
            raise PageTextUnavailable("the page's row could not be read") from None
        try:
            body = self._pages.get(page.storage_key)
        except PageStoreUnavailable:
            raise PageTextUnavailable("the stored page could not be read") from None
        return plain_text(body.decode(errors="replace"))

    def _remove_page_quietly(self, key: str) -> None:
        """Take back an object whose row was not stored; failing to is logged, not raised."""
        try:
            self._pages.remove(key)
        except PageStoreUnavailable as e:
            logger.warning("a page whose row was not stored stays in object storage: %s", e)

    def _result_of(self, run: ResearchRunInput) -> uuid.UUID | None:
        """The report the run succeeded with, or ``None`` while it has no outcome.

        A run that was recorded failed takes no result: the attempt is refused for good.
        """
        try:
            with transaction(self._sessions) as session:
                stored_run = research_run(
                    session,
                    workspace_id=run.workspace_id,
                    project_id=run.project_id,
                    run_id=run.run_id,
                )
        except ResearchRunNotFound as e:
            raise failure(steps.RUN_UNAVAILABLE, str(e)) from None
        if stored_run.status is ResearchRunStatus.FAILED:
            # Its checkpoints were forgotten when the failure was recorded; this removes
            # what the attempt has written since.
            forget_quietly(
                self._checkpoints, run_thread_id(run.run_id), subject=subject_of(run.run_id)
            )
            refused = ResearchRunFinished(run.run_id, stored_run.status)
            raise failure(steps.RUN_UNAVAILABLE, str(refused))
        return stored_run.report_id

    def _store(self, run: ResearchRunInput, final_state: Mapping[str, Any]) -> uuid.UUID:
        """Store the report and mark the run succeeded, both or neither."""
        report = report_of(final_state)
        try:
            with transaction(self._sessions) as session:
                saved = save_research_report(
                    session,
                    workspace_id=run.workspace_id,
                    project_id=run.project_id,
                    run_id=run.run_id,
                    report=report,
                )
                mark_run_succeeded(
                    session,
                    workspace_id=run.workspace_id,
                    project_id=run.project_id,
                    run_id=run.run_id,
                    report_id=saved.id,
                )
        except ResearchRunFinished as e:
            # Another attempt finished the run first; what this one stored was rolled back.
            stored = self._result_of(run)
            if stored is None:
                raise failure(steps.RUN_UNAVAILABLE, str(e)) from None
            return stored
        except (ResearchRunNotFound, ProjectNotFound) as e:
            raise failure(steps.RUN_UNAVAILABLE, str(e)) from None
        except DatabaseError:
            logger.exception("the report of research run %s was not stored", run.run_id)
            raise failure(DATABASE_UNAVAILABLE, steps.NOT_STORED) from None
        return saved.id


class WordedSteps:
    """The steps of the researcher's graph, with a research's failures worded for its run."""

    def __init__(self, graph: AgentGraph) -> None:
        self._graph = graph

    def advance(
        self, run: GraphRunRef, checkpoints: RunCheckpoints, resume: ActionOutcome | None
    ) -> StepOutcome:
        """Take the step, as ``AgentGraph.advance`` says.

        A submission that cannot be read, a request that no longer fits its schema or a
        model whose key is not set ends the research (``RESEARCH_FAILED``); only the model
        provider fails in ways that pass, an outage or a rate limit (``MODEL_UNAVAILABLE``,
        tried again). The message is the error's own, which names no value.
        """
        try:
            return self._graph.advance(run, checkpoints, resume)
        except (*RESEARCH_FAILURES, StoredResearchRequestInvalid, *WORKSPACE_FAILURES) as e:
            logger.info("research run %s failed: %s", run.run_id, e)
            passing = isinstance(e, LLMError) and e.may_pass
            kind = steps.MODEL_UNAVAILABLE if passing else steps.RESEARCH_FAILED
            raise failure(kind, str(e)) from None


def _page_answer(page: ResearchPage) -> Mapping[str, Any]:
    """What the graph is told of a stored page: its id, its address and its flags."""
    return {"page_id": str(page.id), "url": page.url, "flags": list(page.flags)}
