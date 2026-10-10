"""The judge's verdicts as data: what ``judge.py`` produces and ``scoring.py`` reads.

A verdict is stored next to the raw analyzer output it is about, so scores are
recomputed from files. Each one says how it came about: a verdict the judge
did not give in the form asked for is ``malformed`` and carries no answer,
because an answer read into a reply that does not state one would be a guess.

Verdict files are meant to be committed, so a stored verdict holds nothing the
judge may have copied from a repository at length: of a malformed reply only
the parser's own description of what was wrong (``problem``) is stored. The
reply itself stays in memory for the benchmark to put with the excerpts, which
git ignores. ``reason`` is the judge's own short sentence and is stored.

This module holds no logic beyond adding up what the verdicts of a run cost.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class VerdictState(StrEnum):
    JUDGED = "judged"
    MALFORMED = "malformed"  # the judge answered, but not in the form asked for
    FAILED = "failed"  # the model provider did not answer
    NOT_ASKED = "not_asked"  # nothing to judge, or the run's limit of questions was reached


class MatchKind(StrEnum):
    SAME = "same"  # both describe the same capability
    PARTIAL = "partial"  # they overlap, and one covers clearly more or less than the other


class Support(StrEnum):
    SUPPORTED = "supported"
    PARTLY_SUPPORTED = "partly_supported"
    NOT_SUPPORTED = "not_supported"


class Agreement(StrEnum):
    AGREES = "agrees"
    PARTLY_AGREES = "partly_agrees"
    DISAGREES = "disagrees"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Verdict(_Model):
    state: VerdictState
    model: str | None = None  # the model the provider says answered
    cost_usd: float = 0.0
    problem: str | None = None  # why a malformed reply could not be read, in the parser's words
    # What the judge wrote when it was malformed. Never written with the verdict (see above).
    reply: str | None = Field(default=None, exclude=True, repr=False)


class ProposedMatch(_Model):
    """A pair the judge proposes: an expected feature and the drafted feature it corresponds to."""

    expected_id: str
    drafted_id: str
    kind: MatchKind


class FeatureMatchingVerdict(_Verdict):
    proposed: list[ProposedMatch] = Field(default_factory=list)  # in the order the judge gave


class EvidenceVerdict(_Verdict):
    """Whether the lines a claim cites say what the claim says. ``claim`` is the claim's key."""

    claim: str
    support: Support | None = None
    reason: str = ""


class SectionVerdict(_Verdict):
    """Whether a drafted free-text section says what the expected one says."""

    section: str
    agreement: Agreement | None = None
    reason: str = ""


MODEL_ROUTER_ROUTE = "model_router"  # the judge role's model, through the model router


class JudgeVerdicts(_Model):
    """Every verdict about one analyzer run, and by which route the judge was reached.

    Verdicts of different routes are not the same measurement: the settings of
    the judge role apply on one and not on the other. ``cost_known`` is False on
    a route that does not say what a call cost; ``cost_usd`` of each verdict is
    then 0 and means nothing.
    """

    case: str
    run: int
    route: str = MODEL_ROUTER_ROUTE
    cost_known: bool = True
    feature_matching: FeatureMatchingVerdict
    evidence: list[EvidenceVerdict] = Field(default_factory=list)
    sections: list[SectionVerdict] = Field(default_factory=list)

    def _all(self) -> list[_Verdict]:
        return [self.feature_matching, *self.evidence, *self.sections]

    @property
    def cost_usd(self) -> float | None:
        """What the verdicts cost together, or ``None`` on a route that does not say."""
        return sum(verdict.cost_usd for verdict in self._all()) if self.cost_known else None

    @property
    def answered_calls(self) -> int:
        """Judge calls that were answered, whether or not the answer could be used."""
        answered = (VerdictState.JUDGED, VerdictState.MALFORMED)
        return sum(verdict.state in answered for verdict in self._all())

    @property
    def models(self) -> list[str]:
        return sorted({verdict.model for verdict in self._all() if verdict.model})
