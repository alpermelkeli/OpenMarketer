"""What a task shows of its progress, node by node, to the person watching it.

``progress_of`` is the task's progress function (``Agent.progress``): from the name of a node of
``graph.py``, the state it ran on and the changes it answered with, it gives that step's events
(``openmarketer_core.run_events``):

- ``plan``: ``task_planned`` with how many steps and which agents, in order;
- ``check``: ``plan_checked`` with how many steps, whether a person confirms it and how many
  times the plan changed;
- ``confirm``: ``plan_confirmation`` with how the person answered (``confirmed``,
  ``rejected``, ``expired``);
- ``dispatch``: ``step_finished`` with the step's place in the plan (from 1), its agent, its
  run, how it ended and, for a post, how the person decided; and ``step_dropped`` with the
  places of the steps it made impossible;
- ``review``: ``task_reviewed`` with what was decided;
- ``report``: ``task_reported`` with how the task ended and its cost.

``prepare`` shows nothing. That the gate holds ``confirm_plan`` for a person, and how that
ended, the gate records (``proposal_waiting``, ``proposal_decided``, ``proposal_expired``); the
task's end (``finished``, ``failed``) is the workflow's. A step's start is not a node's end, so
it is not here: the run of a step is recorded when the worker prepares it. Only what code
wrote is shown: places, counts, run ids, agents and statuses; no goal, request, step id or
other text a model wrote.

This module is pure: it reads what it is given and imports no framework.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from openmarketer_core.run_events import RunEvent

TASK_PLANNED = "task_planned"  # {steps, agents}
PLAN_CHECKED = "plan_checked"  # {steps, needs_confirmation, plan_changes}
PLAN_CONFIRMATION = "plan_confirmation"  # {status}
STEP_FINISHED = "step_finished"  # {step, agent, run_id, status, post_status?}
STEP_DROPPED = "step_dropped"  # {steps}
TASK_REVIEWED = "task_reviewed"  # {decision}
TASK_REPORTED = "task_reported"  # {outcome, cost_usd}


def progress_of(
    node: str, state: Mapping[str, Any], update: Mapping[str, Any]
) -> Sequence[RunEvent]:
    """The events of the node ``node`` of a task, which ran on ``state``."""
    shown = _SHOWN.get(node)
    return [] if shown is None else shown(state, update)


def _planned(state: Mapping[str, Any], update: Mapping[str, Any]) -> list[RunEvent]:
    steps = update["proposed_plan"]["steps"]
    detail = {"steps": len(steps), "agents": [step["agent"] for step in steps]}
    return [RunEvent(TASK_PLANNED, detail)]


def _checked(state: Mapping[str, Any], update: Mapping[str, Any]) -> list[RunEvent]:
    if "plan" not in update:
        return []
    detail = {
        "steps": len(update["plan"]["steps"]),
        "needs_confirmation": update["needs_confirmation"],
        "plan_changes": update["plan_changes"],
    }
    return [RunEvent(PLAN_CHECKED, detail), *_dropped(state["steps"], update["steps"])]


def _confirmed(state: Mapping[str, Any], update: Mapping[str, Any]) -> list[RunEvent]:
    return [RunEvent(PLAN_CONFIRMATION, {"status": update["confirmation"]})]


def _finished(state: Mapping[str, Any], update: Mapping[str, Any]) -> list[RunEvent]:
    steps = update["steps"]
    place, record = next(
        (index, record)
        for index, record in enumerate(steps)
        if record["id"] == update["finished_step"]
    )
    detail: dict[str, Any] = {
        "step": place + 1,
        "agent": record["agent"],
        "run_id": record["run_id"],
        "status": record["status"],
    }
    post_status = record["outcome"].get("post_status")
    if post_status:
        detail["post_status"] = post_status
    return [RunEvent(STEP_FINISHED, detail), *_dropped(state["steps"], steps)]


def _reviewed(state: Mapping[str, Any], update: Mapping[str, Any]) -> list[RunEvent]:
    review = update.get("review") or {}
    decision = review.get("decision") or "unread"
    return [RunEvent(TASK_REVIEWED, {"decision": decision})]


def _reported(state: Mapping[str, Any], update: Mapping[str, Any]) -> list[RunEvent]:
    detail = {"outcome": update["outcome"], "cost_usd": round(update["total_cost_usd"], 4)}
    return [RunEvent(TASK_REPORTED, detail)]


def _dropped(
    before: Sequence[Mapping[str, Any]], after: Sequence[Mapping[str, Any]]
) -> list[RunEvent]:
    was = {record["id"]: record["status"] for record in before}
    places = [
        index + 1
        for index, record in enumerate(after)
        if record["status"] == "dropped" and was.get(record["id"]) not in (None, "dropped")
    ]
    return [RunEvent(STEP_DROPPED, {"steps": places})] if places else []


_SHOWN = {
    "plan": _planned,
    "check": _checked,
    "confirm": _confirmed,
    "dispatch": _finished,
    "review": _reviewed,
    "report": _reported,
}
