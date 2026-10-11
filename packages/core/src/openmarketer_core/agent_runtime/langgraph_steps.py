"""Taking one step of a LangGraph graph on its thread, and how a node asks for an action.

A node that needs the outside world calls ``request_action``. The graph pauses
there, ``GraphThread.advance`` reports the request as a ``StepOutcome`` that is
waiting for it, and the workflow runs the action and calls ``advance`` again
with the outcome: the graph continues on the same thread, from the checkpoint
it paused at. ``advance`` is safe to call again after an attempt that died: a
paused thread answers with the same request without running anything, and an
outcome that an earlier attempt already applied is not applied twice.

This is the third module that imports LangGraph, with ``graph_checkpoints/`` and
the analyzer's ``graph.py``. The graph is compiled by its owner with the run's
store (``graph_checkpoints/postgres.py`` opens it, and holds the thread for one
attempt). Interrupts, resume values and the graph's state are written by the
store's strict serializer, which rebuilds only plain values, so they must be
plain data: dicts, lists, strings, numbers, booleans and ``None``.

Nothing here decides what an action is allowed to do (``ActionCatalog``), runs
one, retries one or names a thread (``threads.thread_id``). It does not remove
checkpoints either, except when asked to ``forget`` them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, Durability, Interrupt, interrupt
from pydantic import BaseModel

from openmarketer_core.agent_runtime.actions import ActionOutcome, ActionRequest
from openmarketer_core.agent_runtime.steps import (
    MalformedActionRequest,
    MissingInitialState,
    StepOutcome,
    TooManyPendingActions,
    UnexpectedResume,
)
from openmarketer_core.graph_checkpoints import RunCheckpoints

# The checkpoint of a finished step is in the store before the next step starts.
CHECKPOINT_DURABILITY: Durability = "sync"

CompiledGraph = CompiledStateGraph[Any, Any, Any, Any]


def request_action(name: str, payload: BaseModel) -> ActionOutcome:
    """Ask the workflow to run the action ``name`` and wait for what came of it.

    The graph pauses here. The outcome is given back when the thread is
    advanced with it, and a failed action is an outcome like any other: the
    node reads ``succeeded`` and ``error`` and decides what to do.

    Two rules for the node that calls it:

    - One action per node. A node that needs two is two nodes; a graph that
      waits for several actions at once is refused.
    - No side effect before the call. When the outcome arrives the node runs
      again from its start, so anything it did before this line is done twice.
    """
    answer = interrupt({"name": name, "payload": payload.model_dump(mode="json")})
    return ActionOutcome(**answer)


@dataclass(frozen=True)
class ThreadState:
    """What a thread holds: the latest values of the graph's state, and what it waits for."""

    values: dict[str, Any]
    has_checkpoint: bool
    pending_action: ActionRequest | None


class GraphThread:
    """A compiled graph bound to the thread of one run, taken step by step."""

    def __init__(
        self,
        graph: CompiledGraph,
        checkpoints: RunCheckpoints,
        *,
        recursion_limit: int | None = None,
    ) -> None:
        """``graph`` must be compiled with ``checkpointer=checkpoints.store``.

        ``recursion_limit`` bounds the graph steps of one ``advance``; the
        library's own limit applies when it is ``None``.
        """
        if graph.checkpointer is not checkpoints.store:
            raise ValueError("the graph must be compiled with the checkpoint store of the run")
        self._graph = graph
        self._checkpoints = checkpoints
        config: RunnableConfig = {"configurable": {"thread_id": checkpoints.thread_id}}
        if recursion_limit is not None:
            config["recursion_limit"] = recursion_limit
        self._config = config

    def state(self) -> ThreadState:
        """The latest checkpoint of the thread. Raises ``StepError`` when it waits wrongly."""
        snapshot = self._graph.get_state(self._config)
        return ThreadState(
            values=dict(snapshot.values),
            has_checkpoint=snapshot.created_at is not None,
            pending_action=self._single_action(snapshot.interrupts),
        )

    def advance(
        self,
        *,
        initial: Mapping[str, Any] | None = None,
        resume: ActionOutcome | None = None,
    ) -> StepOutcome:
        """Run the graph until it finishes or asks for an action.

        ``resume`` is the outcome of the action the thread waits for. ``initial``
        is the state to start from, and is only used when the thread has no
        checkpoint yet. What happens depends on the thread:

        - waiting for an action, no ``resume``: the same request is returned and
          nothing runs, so an attempt that is repeated learns what to wait for;
        - waiting for an action, and ``resume`` answers it: the graph continues
          with the outcome;
        - ``resume`` answers an action that was asked and is no longer pending: an
          earlier attempt applied it and died before saying so. It is not applied
          again; the graph continues from where it stands, or, when it already
          waits for the next action, that request is returned;
        - no checkpoint and no ``resume``: the graph starts from ``initial``;
        - otherwise: the graph continues from its latest checkpoint. A step that
          was running when an earlier attempt died runs again.

        Raises ``UnexpectedResume`` for an outcome the thread never asked for,
        ``MissingInitialState`` when there is nothing to start from, and
        ``TooManyPendingActions`` or ``MalformedActionRequest`` when the graph
        pauses in a way this runtime does not support.
        """
        state = self.state()
        pending = state.pending_action
        if resume is not None:
            if pending is not None and resume.action_id == pending.action_id:
                return self._run(Command(resume=asdict(resume)))
            if not self._was_asked(resume.action_id):
                raise UnexpectedResume(self._checkpoints.thread_id, resume.action_id)
        if pending is not None:
            return StepOutcome.waiting_for(pending)
        if not state.has_checkpoint:
            if initial is None:
                raise MissingInitialState(self._checkpoints.thread_id)
            return self._run(dict(initial))
        return self._run(None)

    def forget(self) -> None:
        """Remove every checkpoint of the thread: for a run that is over."""
        self._checkpoints.forget()

    def _run(self, graph_input: dict[str, Any] | Command | None) -> StepOutcome:
        output = self._graph.invoke(
            graph_input, self._config, version="v2", durability=CHECKPOINT_DURABILITY
        )
        action = self._single_action(output.interrupts)
        if action is not None:
            return StepOutcome.waiting_for(action)
        return StepOutcome.finished(dict(output.value))

    def _single_action(self, interrupts: tuple[Interrupt, ...]) -> ActionRequest | None:
        thread_id = self._checkpoints.thread_id
        if len(interrupts) > 1:
            raise TooManyPendingActions(thread_id, len(interrupts))
        if not interrupts:
            return None
        asked = interrupts[0]
        value = asked.value
        if not (
            isinstance(value, dict)
            and isinstance(value.get("name"), str)
            and isinstance(value.get("payload"), dict)
        ):
            raise MalformedActionRequest(thread_id)
        return ActionRequest(
            action_id=f"{thread_id}:{asked.id}", name=value["name"], payload=value["payload"]
        )

    def _was_asked(self, action_id: str) -> bool:
        """Whether the thread has asked for ``action_id`` at some point, answered or not.

        It reads the whole history of the thread, which is why it is only asked
        when an outcome is not for the action that is pending.
        """
        prefix = f"{self._checkpoints.thread_id}:"
        if not action_id.startswith(prefix):
            return False
        interrupt_id = action_id.removeprefix(prefix)
        return any(
            asked.id == interrupt_id
            for snapshot in self._graph.get_state_history(self._config)
            for asked in snapshot.interrupts
        )
