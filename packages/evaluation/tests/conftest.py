"""What the evaluation tests share: profiles made by hand, a scripted model, a local repository.

Nothing here uses the network. The model is scripted, and the repository a
case points at is a git folder made for the test.
"""

import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from openmarketer_core.llm import ChatReply
from openmarketer_core.profile import ProductProfile
from openmarketer_evaluation.cases import GoldenCase, LabelProvenance


def _feature(feature_id: str, status: str = "live", **fields: Any) -> dict[str, Any]:
    return {
        "id": feature_id,
        "description": f"Users can {feature_id.replace('-', ' ')}",
        "status": status,
        "evidence": [{"file": "app/Share.kt", "lines": "1-3"}],
        "confidence": 0.8,
        **fields,
    }


def _profile(*features: dict[str, Any], **sections: Any) -> ProductProfile:
    product = {
        "name": "Notes",
        "type": "consumer_app",
        "platforms": ["android", "ios"],
        "evidence": [{"file": "README.md", "lines": "1-3"}],
        "confidence": 0.9,
    }
    product.update(sections.pop("product", {}))
    return ProductProfile.model_validate(
        {"product": product, "features": list(features), **sections}
    )


@pytest.fixture
def feature() -> Callable[..., dict[str, Any]]:
    """Make a feature by id; ``live`` and evidenced unless told otherwise."""
    return _feature


@pytest.fixture
def profile() -> Callable[..., ProductProfile]:
    """Make a profile of the product "Notes" with the given features and sections."""
    return _profile


class ScriptedModel:
    """Replays prepared replies in order and records what it was asked.

    A reply is text, a list of tool calls, or an exception to raise.
    """

    def __init__(self, *replies: Any, cost: float = 0.01, model: str = "scripted/model") -> None:
        self.replies = list(replies)
        self.cost = cost
        self.model = model
        self.requests: list[dict[str, Any]] = []

    def chat(self, role, messages, *, tools=None, tool_choice=None) -> ChatReply:
        self.requests.append({"role": role, "messages": [dict(m) for m in messages]})
        if not self.replies:
            raise AssertionError("the scripted model was asked more often than it was scripted for")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, str):
            message = {"role": "assistant", "content": reply}
        else:
            message = {"role": "assistant", "content": None, "tool_calls": reply}
        return ChatReply(message=message, model=self.model, cost_usd=self.cost)

    def questions(self) -> list[str]:
        """The user message of every request, in order."""
        return [request["messages"][-1]["content"] for request in self.requests]


@pytest.fixture
def scripted() -> type[ScriptedModel]:
    return ScriptedModel


def _submission(profile: ProductProfile) -> list[dict[str, Any]]:
    arguments = json.dumps(profile.model_dump(mode="json"))
    return [
        {
            "id": "c1",
            "type": "function",
            "function": {"name": "submit_profile", "arguments": arguments},
        }
    ]


@pytest.fixture
def submission() -> Callable[[ProductProfile], list[dict[str, Any]]]:
    """The analyzer turn that submits a profile."""
    return _submission


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com",
         "-c", "commit.gpgsign=false", *args],
        cwd=repo, check=True, capture_output=True, text=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
    )  # fmt: skip
    return done.stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> tuple[Path, str]:
    """A local git repository whose branch has moved on, and the commit before it did."""
    repo = tmp_path / "source"
    files = {
        "README.md": "# Notes\n\nShare memories offline.\n",
        "app/Share.kt": "\n".join(f"share line {i}" for i in range(1, 201)) + "\n",
    }
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "first")
    pinned = _git(repo, "rev-parse", "HEAD")
    (repo / "README.md").write_text("# Renamed since\n")
    _git(repo, "commit", "-q", "-am", "second")
    return repo, pinned


@pytest.fixture
def golden_case(repository, profile, feature) -> GoldenCase:
    """A case pinned to the first commit of ``repository``, expecting two live features."""
    repo, pinned = repository
    return GoldenCase(
        name="notes",
        repository=str(repo),
        commit=pinned,
        label_provenance=LabelProvenance.WRITTEN_BY_PERSON,
        notes="",
        expected=profile(feature("share-memories"), feature("pair-devices")),
        expected_sha256="0" * 64,
    )
