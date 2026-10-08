"""Tests for the intake stage. They use real git and gitleaks on a local repository."""

import base64
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
        "config/settings.py": (
            "DEBUG = False\n"
            f'GITHUB_TOKEN = "{FAKE_TOKEN}"  # gitleaks:allow\n'
            f'SAME_AGAIN = "{FAKE_TOKEN}"\n'
            "TIMEOUT = 30\n"
        ),
        "config/blob.txt": base64.b64encode(f'token = "{FAKE_TOKEN}"'.encode()).decode() + "\n",
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
    assert [(f.file, f.start_line, f.redacted) for f in intake.findings] == [
        ("config/blob.txt", 1, False),
        ("config/settings.py", 2, True),
        ("config/settings.py", 3, True),
    ]
    assert all(f.rule_id for f in intake.findings)


@needs_gitleaks
def test_findings_do_not_carry_the_secret(intake):
    assert FAKE_TOKEN not in repr(intake.findings)


@needs_gitleaks
def test_secret_is_blanked_out_and_the_rest_of_the_file_stays_readable(intake):
    assert intake.files.read_text("config/settings.py") == (
        "DEBUG = False\n"
        'GITHUB_TOKEN = "[REDACTED]"  # gitleaks:allow\n'
        'SAME_AGAIN = "[REDACTED]"\n'
        "TIMEOUT = 30\n"
    )


@needs_gitleaks
def test_secret_does_not_survive_anywhere_in_the_readable_tree(intake):
    for path in intake.files:
        assert FAKE_TOKEN.encode() not in intake.files.read_bytes(path), path


@needs_gitleaks
def test_file_whose_secret_cannot_be_blanked_out_is_excluded(intake):
    # The token sits inside a base64 blob: gitleaks finds it, but it is not literally in the file.
    assert intake.files.exclusion("config/blob.txt") is Exclusion.SECRET_FOUND
    with pytest.raises(ExcludedFileError, match="secret_found"):
        intake.files.read_text("config/blob.txt")


@needs_gitleaks
def test_multi_line_secret_keeps_line_numbers(tmp_path):
    from openmarketer_core.intake.secrets import scan_and_redact

    body = "\n".join(
        "".join(random.Random(i).choices(string.ascii_letters + string.digits, k=64))
        for i in range(12)
    )
    key = "-----BEGIN " + "RSA PRIVATE KEY-----\n" + body + "\n-----END " + "RSA PRIVATE KEY-----"
    (tmp_path / "deploy.py").write_text(f'BEFORE = 1\nKEY = """{key}"""\nAFTER = 2\n')
    findings = scan_and_redact(tmp_path)
    text = (tmp_path / "deploy.py").read_text()
    assert findings and all(f.redacted for f in findings)
    assert body.splitlines()[3] not in text
    assert text.splitlines()[15] == "AFTER = 2"


@needs_gitleaks
def test_readable_files_after_intake(intake):
    assert list(intake.files) == [
        ".gitleaks.toml",
        ".gitleaksignore",
        "README.md",
        "hosts-link",
        "config/settings.py",
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
