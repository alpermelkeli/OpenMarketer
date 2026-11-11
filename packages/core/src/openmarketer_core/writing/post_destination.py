"""Where a proposed post goes, read from the proposal a person decides it on.

A writing run's ``publish_post`` proposal names the account the post would be published from
and what publishing it is reckoned to cost, or names none, and the post is recorded as a dry
run. A screen that shows the proposal reads that here instead of in the payload's keys, so the
payload's shape stays the writer's (``writer_agent/definitions.py``).

This module reads a stored payload; it decides nothing about it, and approving it is a person's
decision on the proposal.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from pydantic import ValidationError

from openmarketer_core.writing.workflow_contract import WRITING_RUN_KIND
from openmarketer_core.writing.writer_agent.definitions import PUBLISH_POST, PublishPost


@dataclass(frozen=True)
class PostDestination:
    """The account a post is published from, with its estimate; all ``None`` for a dry run."""

    account_id: str | None
    username: str | None
    cost_estimate_usd: Decimal | None

    @property
    def live(self) -> bool:
        """Whether approving the post publishes it, rather than recording it as a dry run."""
        return self.account_id is not None


def post_destination(
    run_kind: str, action_name: str, payload: Mapping[str, Any]
) -> PostDestination | None:
    """Where the proposed post goes; ``None`` for a proposal that is not a writer's post.

    A payload that no longer reads as ``publish_post`` reads (one a later version tightened) is
    ``None`` too: nothing is said about where it goes rather than something wrong.
    """
    if run_kind != WRITING_RUN_KIND or action_name != PUBLISH_POST:
        return None
    try:
        post = PublishPost.model_validate(payload)
    except ValidationError:
        return None
    return PostDestination(
        account_id=post.account_id,
        username=post.username,
        cost_estimate_usd=post.cost_estimate_usd,
    )
