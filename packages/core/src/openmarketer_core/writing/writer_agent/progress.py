"""What a writing run shows of its progress, node by node, to the person watching it.

``progress_of`` is the writer's progress function (``Agent.progress``): from the name of a node
of ``graph.py``, the state it ran on and the changes it answered with, it gives that step's
events (``openmarketer_core.run_events``):

- ``prepare``: ``writing_prepared`` with the profile version and how many live features the
  run may write about;
- ``write`` and ``revise``: ``variants_written`` with the round and how many variants;
- ``check``: ``variants_checked`` with the round, how many kept every code rule and the rules
  that were broken;
- ``decide``: ``variant_chosen`` with the round, the variant and how many numbers the profile
  does not hold it has, or ``revising`` with the round it rewrites;
- ``store``: ``draft_stored`` with the content item the draft was stored as;
- ``record``: ``post_recorded`` with how the post ended.

``propose`` shows nothing: the gate records that the action waits for a person and how that
ended (``proposal_waiting``, ``proposal_decided``, ``proposal_expired``), and the run's end
(``finished``, ``failed``) is the workflow's. Only structured data is shown: ids, counts and
names written by code. No text a model wrote, no note and no feature description is shown.

The kinds are the writer's own, as ``RunEvent`` allows; they are named here because no other
run records them.

This module is pure: it reads what it is given and imports no framework.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from openmarketer_core.run_events import RunEvent

WRITING_PREPARED = "writing_prepared"  # {profile_version_id, features}
VARIANTS_WRITTEN = "variants_written"  # {round, variants}
VARIANTS_CHECKED = "variants_checked"  # {round, kept, broken}
VARIANT_CHOSEN = "variant_chosen"  # {round, variant, flagged_numbers}
REVISING = "revising"  # {round}
DRAFT_STORED = "draft_stored"  # {content_item_id}
POST_RECORDED = "post_recorded"  # {status}


def progress_of(
    node: str, state: Mapping[str, Any], update: Mapping[str, Any]
) -> Sequence[RunEvent]:
    """The events of the node ``node`` of a writing run, which ran on ``state``."""
    shown = _SHOWN.get(node)
    return [] if shown is None else shown(state, update)


def _prepared(state: Mapping[str, Any], update: Mapping[str, Any]) -> list[RunEvent]:
    detail = {
        "profile_version_id": update["profile_version_id"],
        "features": len(update["writing_context"]["features"]),
    }
    return [RunEvent(WRITING_PREPARED, detail)]


def _written(state: Mapping[str, Any], update: Mapping[str, Any]) -> list[RunEvent]:
    detail = {"round": update["round"], "variants": len(update["variants"])}
    return [RunEvent(VARIANTS_WRITTEN, detail)]


def _checked(state: Mapping[str, Any], update: Mapping[str, Any]) -> list[RunEvent]:
    checks = update["checks"]
    broken = sorted({f["rule"] for check in checks for f in check["failures"]})
    detail = {
        "round": state["round"],
        "kept": sum(1 for check in checks if check["passed"]),
        "broken": broken,
    }
    return [RunEvent(VARIANTS_CHECKED, detail)]


def _decided(state: Mapping[str, Any], update: Mapping[str, Any]) -> list[RunEvent]:
    if update["decision"] == "revise":
        return [RunEvent(REVISING, {"round": state["round"] + 1})]
    detail = {
        "round": state["round"],
        "variant": update["chosen"],
        "flagged_numbers": len(state["checks"][update["chosen"]]["flagged_numbers"]),
    }
    return [RunEvent(VARIANT_CHOSEN, detail)]


def _stored(state: Mapping[str, Any], update: Mapping[str, Any]) -> list[RunEvent]:
    return [RunEvent(DRAFT_STORED, {"content_item_id": update["content_item_id"]})]


def _recorded(state: Mapping[str, Any], update: Mapping[str, Any]) -> list[RunEvent]:
    return [RunEvent(POST_RECORDED, {"status": update["result"]["status"]})]


_SHOWN = {
    "prepare": _prepared,
    "write": _written,
    "revise": _written,
    "check": _checked,
    "decide": _decided,
    "store": _stored,
    "record": _recorded,
}
