"""Tests for the intake stage. They use real git and gitleaks on a local repository."""

import os
import random
import shutil
import string
import subprocess
from pathlib import Path

import pytest

from openmarketer_core.intake import (
    ExcludedFileError,
    Exclusion,
    IntakeError,
    RepoFiles,
    run_intake,
)
from openmarketer_core.intake.git import _git_env, clone

needs_gitleaks = pytest.mark.skipif(shutil.which("gitleaks") is None, reason="needs gitleaks")

# Built at run time so that this file itself contains no secret-shaped string.
FAKE_TOKEN = "ghp_" + "".join(random.Random(7).choices(string.ascii_letters + string.digits, k=36))


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com",
         "-c", "commit.gpgsign=false", *args],
        cwd=repo, check=True, capture_output=True, text=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
    )  # fmt: skip
    return done.stdout.strip()


@pytest.fixture
def source_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "source"
    files = {
        "README.md": "# Example App\n",
        "src/app.py": "print('hello')\n",
        ".env": "API_KEY=local-value\n",
        ".env.production": "API_KEY=prod-value\n",
        "certs/server.pem": "not really a key\n",
        "ios/AuthKey.p8": "not really a key\n",
        "node_modules/left-pad/index.js": "module.exports = 1\n",
        "config/settings.py": f'GITHUB_TOKEN = "{FAKE_TOKEN}"  # gitleaks:allow\n',
        # A repository must not be able to switch the scan off:
        ".gitleaks.toml": '[allowlist]\npaths = [".*"]\n',
        ".gitleaksignore": "config/settings.py\n",
    }
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    (repo / "hosts-link").symlink_to("/etc/hosts")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A", "-f")
    git(repo, "commit", "-q", "-m", "first")
    (repo / "untracked-notes.txt").write_text("never committed\n")
    return repo


@pytest.fixture
def intake(source_repo: Path, tmp_path: Path):
    return run_intake(str(source_repo), tmp_path / "clone")


# ------------------------------------------------------------------ clone
def test_clone_records_the_commit(source_repo, tmp_path):
    snapshot = clone(str(source_repo), tmp_path / "clone")
    assert snapshot.commit_sha == git(source_repo, "rev-parse", "HEAD")
    assert snapshot.ref == "main"
    assert (snapshot.root / "README.md").read_text() == "# Example App\n"


def test_clone_contains_only_committed_files(source_repo, tmp_path):
    snapshot = clone(str(source_repo), tmp_path / "clone")
    assert not (snapshot.root / "untracked-notes.txt").exists()


def test_clone_is_shallow(source_repo, tmp_path):
    (source_repo / "README.md").write_text("# v2\n")
    git(source_repo, "commit", "-q", "-am", "second")
    snapshot = clone(str(source_repo), tmp_path / "clone")
    assert git(snapshot.root, "rev-list", "--count", "HEAD") == "1"


def test_symlinks_are_not_followed(source_repo, tmp_path):
    snapshot = clone(str(source_repo), tmp_path / "clone")
    link = snapshot.root / "hosts-link"
    assert not link.is_symlink()
    assert link.read_text() == "/etc/hosts"


@pytest.mark.parametrize(
    "source",
    [
        "ext::sh -c 'touch /tmp/pwned'",
        "http://example.com/repo.git",
        "ssh://git@example.com/repo.git",
        "git@example.com:org/repo.git",
        "https://user:secret@example.com/repo.git",
        "--upload-pack=touch /tmp/pwned",
        "https:///repo.git",
    ],
)
def test_unsafe_sources_are_rejected(source, tmp_path):
    with pytest.raises(IntakeError):
        clone(source, tmp_path / "clone")
    assert not (tmp_path / "clone").exists()


def test_local_folder_must_be_a_git_repository(tmp_path):
    (tmp_path / "plain").mkdir()
    with pytest.raises(IntakeError, match="not a git repository"):
        clone(str(tmp_path / "plain"), tmp_path / "clone")


def test_destination_must_be_empty(source_repo, tmp_path):
    dest = tmp_path / "clone"
    dest.mkdir()
    (dest / "existing.txt").write_text("x")
    with pytest.raises(IntakeError, match="not empty"):
        clone(str(source_repo), dest)


def test_token_is_passed_through_the_environment_only():
    env = _git_env("s3cret-token")
    assert env["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    assert env["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic ")
    assert "s3cret-token" not in env["GIT_CONFIG_VALUE_0"]  # base64, not plain text
    assert "GIT_CONFIG_COUNT" not in _git_env(None)


def test_user_git_configuration_is_ignored():
    env = _git_env(None)
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_TERMINAL_PROMPT"] == "0"


# ------------------------------------------------------------ secret scan
@needs_gitleaks
def test_secret_is_found_despite_repository_allowlists(intake):
    assert [(f.file, f.start_line) for f in intake.findings] == [("config/settings.py", 1)]
    assert intake.findings[0].rule_id


@needs_gitleaks
def test_findings_do_not_carry_the_secret(intake):
    assert FAKE_TOKEN not in repr(intake.findings)


@needs_gitleaks
def test_file_with_a_secret_cannot_be_read(intake):
    assert intake.files.exclusion("config/settings.py") is Exclusion.SECRET_FOUND
    with pytest.raises(ExcludedFileError, match="secret_found"):
        intake.files.read_text("config/settings.py")


@needs_gitleaks
def test_readable_files_after_intake(intake):
    assert list(intake.files) == [
        ".gitleaks.toml",
        ".gitleaksignore",
        "README.md",
        "hosts-link",
        "src/app.py",
    ]
    assert intake.files.read_text("src/app.py") == "print('hello')\n"


# ------------------------------------------------------------- exclusions
@pytest.fixture
def files(source_repo, tmp_path) -> RepoFiles:
    return RepoFiles(clone(str(source_repo), tmp_path / "clone").root)


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        (".env", Exclusion.ENV_FILE),
        (".env.production", Exclusion.ENV_FILE),
        ("certs/server.pem", Exclusion.KEY_FILE),
        ("ios/AuthKey.p8", Exclusion.KEY_FILE),
        ("node_modules/left-pad/index.js", Exclusion.BUILD_OUTPUT),
        (".git/config", Exclusion.VERSION_CONTROL),
        ("../source/README.md", Exclusion.OUTSIDE_REPOSITORY),
        ("/etc/hosts", Exclusion.OUTSIDE_REPOSITORY),
        ("", Exclusion.OUTSIDE_REPOSITORY),
    ],
)
def test_excluded_paths_cannot_be_read(files, path, reason):
    assert files.exclusion(path) is reason
    with pytest.raises(ExcludedFileError):
        files.read_bytes(path)


def test_key_file_names_are_matched_case_insensitively(files):
    assert files.exclusion("Certs/Server.PEM") is Exclusion.KEY_FILE
    assert files.exclusion("deploy/ID_RSA") is Exclusion.KEY_FILE


def test_symlink_added_after_clone_is_refused(files):
    (files.root / "sneaky").symlink_to("/etc/hosts")
    assert files.exclusion("sneaky") is Exclusion.OUTSIDE_REPOSITORY
    assert "sneaky" not in list(files)


def test_listing_never_includes_excluded_files(files):
    listed = list(files)
    assert "README.md" in listed
    assert not any(files.exclusion(path) for path in listed)
    assert not any(p.startswith((".env", ".git/", "node_modules/")) for p in listed)
