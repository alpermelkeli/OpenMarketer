"""What a studio run shows of its progress, node by node, to the person watching its chat.

``progress_of`` is the studio's progress function (``Agent.progress``): from the name of a node
of ``graph.py``, the state it ran on and the changes it answered with, it gives that step's
events (``openmarketer_core.run_events``):

- ``brief``: ``briefed`` with the first technique;
- ``operate``: ``produced`` with the operation, the version it made and whether it made one;
- ``check``: ``checked`` with the candidate, whether it passed and the failures' keys;
- ``verify``: ``verified`` with the candidate, whether it passed and the criteria it failed;
- ``decide``: ``technique_switched`` when it changed the technique, and nothing otherwise;
- ``edit_plan``: ``edit_planned`` with the round it plans and how many steps it has;
- ``finalize`` and ``escalate``: ``finalized`` or ``escalated`` with the version the run ends
  with (and, for an escalation, the code's sentence of why).

``produce`` shows nothing: what it chose is shown when ``operate`` has run it. Only structured
data is shown: ids, names, counts and keys written by code. No prompt, page, program, OCR text
or error a handler or a model wrote is ever shown.

This module is pure: it reads what it is given and imports no framework.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from openmarketer_core.run_events import (
    BRIEFED,
    CHECKED,
    EDIT_PLANNED,
    ESCALATED,
    FINALIZED,
    PRODUCED,
    TECHNIQUE_SWITCHED,
    VERIFIED,
    RunEvent,
)
from openmarketer_core.studio.studio_agent.rules import VERIFIER_PREFIX, verifier_failures


def progress_of(
    node: str, state: Mapping[str, Any], update: Mapping[str, Any]
) -> Sequence[RunEvent]:
    """The events of the node ``node`` of a studio run, which ran on ``state``."""
    shown = _SHOWN.get(node)
    return [] if shown is None else shown(state, update)


def _briefed(state: Mapping[str, Any], update: Mapping[str, Any]) -> list[RunEvent]:
    return [RunEvent(BRIEFED, {"technique": update["technique"]})]


def _produced(state: Mapping[str, Any], update: Mapping[str, Any]) -> list[RunEvent]:
    pending = state["pending"]
    made = len(update.get("versions", state["versions"])) > len(state["versions"])
    detail = {
        "operation": pending["op"],
        "version_id": pending["version_id"] if made else None,
        "technique": pending["technique"],
        "round": state["round"],
        "ok": made,
    }
    return [RunEvent(PRODUCED, detail)]


def _checked(state: Mapping[str, Any], update: Mapping[str, Any]) -> list[RunEvent]:
    check = update["check"]
    detail = {
        "version_id": check["version_id"],
        "passed": check["passed"],
        "failures": check["failures"],
    }
    return [RunEvent(CHECKED, detail)]


def _verified(state: Mapping[str, Any], update: Mapping[str, Any]) -> list[RunEvent]:
    failed = [
        key.removeprefix(VERIFIER_PREFIX) for key in verifier_failures(update["verification"])
    ]
    detail = {"version_id": state["candidate"], "passed": not failed, "failed": failed}
    return [RunEvent(VERIFIED, detail)]


def _decided(state: Mapping[str, Any], update: Mapping[str, Any]) -> list[RunEvent]:
    switched_to = update.get("technique", state["technique"])
    if switched_to == state["technique"]:
        return []
    detail = {
        "from_technique": state["technique"],
        "to_technique": switched_to,
        "round": state["round"],
    }
    return [RunEvent(TECHNIQUE_SWITCHED, detail)]


def _edit_planned(state: Mapping[str, Any], update: Mapping[str, Any]) -> list[RunEvent]:
    detail = {"round": update["round"], "steps": len(update["edit_plan"]["steps"])}
    return [RunEvent(EDIT_PLANNED, detail)]


def _finalized(state: Mapping[str, Any], update: Mapping[str, Any]) -> list[RunEvent]:
    result = update["result"]
    return [
        RunEvent(FINALIZED, {"version_id": result["final_version_id"], "round": state["round"]})
    ]


def _escalated(state: Mapping[str, Any], update: Mapping[str, Any]) -> list[RunEvent]:
    result = update["result"]
    detail = {"version_id": result["final_version_id"], "reason": result["reason"]}
    return [RunEvent(ESCALATED, detail)]


_SHOWN = {
    "brief": _briefed,
    "operate": _produced,
    "check": _checked,
    "verify": _verified,
    "decide": _decided,
    "edit_plan": _edit_planned,
    "finalize": _finalized,
    "escalate": _escalated,
}
