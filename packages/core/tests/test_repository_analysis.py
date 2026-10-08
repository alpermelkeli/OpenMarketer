"""Tests for the whole pipeline on a local repository, with a scripted model.

They use real git and gitleaks; no network and no extractor plugins.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from openmarketer_core.analyzer.rules import SUBMIT
from openmarketer_core.extraction import Fact
from openmarketer_core.intake import IntakeError, RepoFiles
from openmarketer_core.llm import ChatReply
from openmarketer_core.repository_analysis import analyze_repository

pytestmark = pytest.mark.skipif(shutil.which("gitleaks") is None, reason="needs gitleaks")

PROFILE = {
    "product": {
        "name": "Example App",
        "type": "dev_tool",
        "evidence": [{"file": "README.md", "lines": "1-1"}],
        "confidence": 0.9,
    }
}


class SubmitsAtOnce:
    """A model whose first turn submits ``PROFILE``."""

    def chat(self, role, messages, *, tools=None, tool_choice=None) -> ChatReply:
        submit = {
            "id": "call-1",
            "type": "function",
            "function": {"name": SUBMIT, "arguments": json.dumps(PROFILE)},
        }
        message = {"role": "assistant", "content": None, "tool_calls": [submit]}
        return ChatReply(message=message, model="fake/model", cost_usd=0.0)


class ReadmeTitle:
    name = "readme_title"

    def extract(self, files: RepoFiles) -> list[Fact]:
        title = files.read_text("README.md").splitlines()[0]
        return [Fact(kind="readme.title", value=title, file="README.md", start_line=1)]


@pytest.fixture
def source_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "source"
    repo.mkdir()
    (repo / "README.md").write_text("# Example App\n")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    for args in (
        ["init", "-q", "-b", "main"],
        ["add", "-A"],
        ["-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "first"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, env=env)
    return repo


def test_repository_is_cloned_extracted_and_analysed(source_repo, tmp_path):
    result = analyze_repository(
        str(source_repo), tmp_path / "clone", model=SubmitsAtOnce(), extractors=[ReadmeTitle()]
    )
    assert result.intake.snapshot.ref == "main"
    assert [(f.extractor, f.value) for f in result.extraction.facts] == [
        ("readme_title", "# Example App")
    ]
    assert result.analysis.profile.product.name == "Example App"


def test_repository_that_cannot_be_cloned_is_an_intake_error(tmp_path):
    with pytest.raises(IntakeError):
        analyze_repository(
            str(tmp_path / "missing"), tmp_path / "clone", model=SubmitsAtOnce(), extractors=[]
        )
