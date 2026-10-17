"""The questions a decision model is asked, as plain data that domain rules can build.

This module is the vocabulary only: it imports no HTTP client and knows no endpoint. The
port that answers the questions and its adapter are in ``decisions.py``.
"""

from dataclasses import dataclass
from typing import Any

YES_OR_NO = "noul"


@dataclass(frozen=True)
class YesNoQuestion:
    """One question about the state, and what each answer means to whoever asks."""

    instructions: str
    true_means: str
    false_means: str

    def as_request(self) -> dict[str, Any]:
        """The question in the endpoint's own form."""
        return {
            "type": YES_OR_NO,
            "instructions": self.instructions,
            "criteria": {"true": self.true_means, "false": self.false_means},
        }
