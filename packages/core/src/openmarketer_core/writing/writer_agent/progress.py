"""What a writing run shows of its progress, node by node, to the person watching it.

``progress_of`` is the writer's progress function (``Agent.progress``): from the name of a node
of ``graph.py``, the state it ran on and the changes it answered with, it gives that step's
events (``openmarketer_core.run_events``):

- ``prepare``: ``writing_prepared`` with the profile version and how many live features the
  run may write about;
- ``plan``: ``post_planned`` with the format, the features it rests on and how many changes
  code made to it;
- ``draft`` and ``revise``: ``variants_written`` with the round and how many variants;
- ``check``: ``variants_checked`` with the round, how many kept every code rule and the rules
  that were broken;
- ``critique``: ``variants_critiqued`` with the round, how many the critic reviewed and passed,
  and how many made a risky claim; nothing when the critic was not asked;
- ``decide``: ``variant_chosen`` with the round, the variant and whether the critic passed it,
  or ``revising`` with the round it rewrites;
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
from openmarketer_core.writing.writer_agent.rules import critic_passes

WRITING_PREPARED = "writing_prepared"  # {profile_version_id, features}
POST_PLANNED = "post_planned"  # {format, features, changes}
VARIANTS_WRITTEN = "variants_written"  # {round, variants}
VARIANTS_CHECKED = "variants_checked"  # {round, kept, broken}
VARIANTS_CRITIQUED = "variants_critiqued"  # {round, reviewed, passed, risky}
VARIANT_CHOSEN = "variant_chosen"  # {round, variant, passed_critic}
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


def _planned(state: Mapping[str, Any], update: Mapping[str, Any]) -> list[RunEvent]:
    plan = update["plan"]
    detail = {
        "format": plan["format"],
        "features": plan["feature_ids"],
        "changes": len(update["plan_changes"]),
    }
    return [RunEvent(POST_PLANNED, detail)]


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


def _critiqued(state: Mapping[str, Any], update: Mapping[str, Any]) -> list[RunEvent]:
    reviews = update["critique"].get("reviews", [])
    if not reviews:
        return []
    detail = {
        "round": state["round"],
        "reviewed": len(reviews),
        "passed": sum(1 for review in reviews if critic_passes(review)),
        "risky": sum(1 for review in reviews if review["risky_claim"]),
    }
    return [RunEvent(VARIANTS_CRITIQUED, detail)]


def _decided(state: Mapping[str, Any], update: Mapping[str, Any]) -> list[RunEvent]:
    if update["decision"] == "revise":
        return [RunEvent(REVISING, {"round": state["round"] + 1})]
    detail = {
        "round": state["round"],
        "variant": update["chosen"],
        "passed_critic": update["passed_critic"],
    }
    return [RunEvent(VARIANT_CHOSEN, detail)]


def _stored(state: Mapping[str, Any], update: Mapping[str, Any]) -> list[RunEvent]:
    return [RunEvent(DRAFT_STORED, {"content_item_id": update["content_item_id"]})]


def _recorded(state: Mapping[str, Any], update: Mapping[str, Any]) -> list[RunEvent]:
    return [RunEvent(POST_RECORDED, {"status": update["result"]["status"]})]


_SHOWN = {
    "prepare": _prepared,
    "plan": _planned,
    "draft": _written,
    "revise": _written,
    "check": _checked,
    "critique": _critiqued,
    "decide": _decided,
    "store": _stored,
    "record": _recorded,
}
