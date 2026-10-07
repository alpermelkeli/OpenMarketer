"""The only way later stages read files of the analysed repository.

``RepoFiles`` lists and reads files, and refuses anything that must never be
read or sent to a model: ``.env*`` files, key material, build output, version
control data and every file in which the secret scan found something.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Iterator
from enum import StrEnum
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath

from openmarketer_core.intake.errors import ExcludedFileError


class Exclusion(StrEnum):
    ENV_FILE = "env_file"
    KEY_FILE = "key_file"
    BUILD_OUTPUT = "build_output"
    VERSION_CONTROL = "version_control"
    SECRET_FOUND = "secret_found"
    OUTSIDE_REPOSITORY = "outside_repository"


KEY_FILE_PATTERNS = (
    "*.pem", "*.key", "*.p8", "*.p12", "*.pfx", "*.ppk", "*.jks", "*.keystore",
    "*.mobileprovision", "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*",
)  # fmt: skip

BUILD_OUTPUT_DIRS = frozenset({
    "node_modules", "build", "dist", "out", "target", ".next", ".nuxt", ".gradle",
    ".dart_tool", "Pods", "DerivedData", "__pycache__", ".venv", "venv",
})  # fmt: skip


def exclusion_by_name(rel: PurePosixPath) -> Exclusion | None:
    """Exclusion that follows from the path alone."""
    *dirs, name = rel.parts
    if ".git" in rel.parts:
        return Exclusion.VERSION_CONTROL
    if any(d in BUILD_OUTPUT_DIRS for d in dirs):
        return Exclusion.BUILD_OUTPUT
    lowered = name.lower()
    if lowered.startswith(".env"):
        return Exclusion.ENV_FILE
    if any(fnmatch(lowered, pattern) for pattern in KEY_FILE_PATTERNS):
        return Exclusion.KEY_FILE
    return None


class RepoFiles:
    """Read access to a repository snapshot with the exclusion rules applied."""

    def __init__(self, root: Path, secret_files: Iterable[str] = ()) -> None:
        self.root = root.resolve()
        self._secret_files = frozenset(secret_files)

    def exclusion(self, rel_path: str) -> Exclusion | None:
        """Why ``rel_path`` may not be read, or ``None`` if it may."""
        rel = PurePosixPath(rel_path)
        if rel.is_absolute() or ".." in rel.parts or not rel.parts:
            return Exclusion.OUTSIDE_REPOSITORY
        path = self.root / rel
        if path.is_symlink() or not path.resolve().is_relative_to(self.root):
            return Exclusion.OUTSIDE_REPOSITORY
        if rel.as_posix() in self._secret_files:
            return Exclusion.SECRET_FOUND
        return exclusion_by_name(rel)

    def __iter__(self) -> Iterator[str]:
        """Relative paths of every readable file, in a stable order."""
        for current, dirs, names in os.walk(self.root):
            base = Path(current).relative_to(self.root)
            dirs[:] = sorted(
                d
                for d in dirs
                if d != ".git" and d not in BUILD_OUTPUT_DIRS and not Path(current, d).is_symlink()
            )
            for name in sorted(names):
                rel = (base / name).as_posix()
                if self.exclusion(rel) is None:
                    yield rel

    def read_bytes(self, rel_path: str) -> bytes:
        reason = self.exclusion(rel_path)
        if reason is not None:
            raise ExcludedFileError(f"{rel_path}: excluded ({reason.value})")
        return (self.root / rel_path).read_bytes()

    def read_text(self, rel_path: str) -> str:
        return self.read_bytes(rel_path).decode("utf-8", errors="replace")
