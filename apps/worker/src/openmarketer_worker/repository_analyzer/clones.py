"""``LocalClones``: the life of a run's clone on the disk of this worker.

An analysis reads one commit of a repository for as long as it runs: the clone is made by
an activity of its own, the steps of the graph that follow read it, and it is removed when
the run ends. This module keeps that clone: one folder per run under one root, named by the
run's id and by nothing a repository or a URL says, readable by the worker's user only.

What is kept of a clone is the result of intake (``IntakeResult``), so the repository is
still read only through ``RepoFiles``, after the secret scan redacted it. It is kept in
memory, and the disk is a worker's own: a step taken by another worker, or by this one after
a restart, finds no clone, and ``at_commit`` then fetches the commit the run reads again. The
commit is the run's, in its graph's state, so every attempt reads the same one.

Handlers and steps run in threads, and a thread that Temporal gave up on cannot be stopped.
So everything here is safe to call from several threads: one thread at a time works on the
clone of a run, and a run that is released while its clone is still being made is removed
by the thread that makes it, when it is done.

What a run leaves when its worker dies is removed by ``sweep`` when a worker starts.

This module holds no rule about what may be cloned (``intake`` does, and the caller names
the source) and decides nothing about when a run ends: its caller releases the run. It
resolves no credential either: the caller says how the tokens of the run are found, and they
are asked for only when a clone is really made, handed to intake and not kept.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

from openmarketer_core.repository_analysis.intake import IntakeResult, RepositoryTokens

logger = logging.getLogger(__name__)

# Clone the source into the folder and return a safe view of it; of the default branch, or of
# the commit named. It is ``run_intake``, with the tokens it may offer to their hosts.
Intake = Callable[[str, Path, str | None, RepositoryTokens], IntakeResult]
# The tokens a clone of the run may carry, resolved when asked: the run's workspace can be
# given one while the run waits.
TokensOfTheRun = Callable[[], RepositoryTokens]

OWNER_ONLY = 0o700
# The folder in a run's folder that git clones into: git wants one that is empty or not there.
REPOSITORY_FOLDER = "repo"


class LocalClones:
    """The clones of the runs this worker is working on, by run id."""

    def __init__(self, root: Path, intake: Intake) -> None:
        """``root`` is made if it is not there, for the worker's user only.

        Raises ``OSError`` when it cannot be made, and ``PermissionError`` when something
        else is there or it is another user's: the system's temporary folder is shared, and a
        folder somebody else made is one they can read and write.
        """
        root.mkdir(mode=OWNER_ONLY, parents=True, exist_ok=True)
        found = root.lstat()
        if root.is_symlink() or not root.is_dir() or found.st_uid != os.getuid():
            raise PermissionError(
                f"{root} is not a folder of the user the worker runs as: remove it, "
                "and the worker makes its own"
            )
        root.chmod(OWNER_ONLY)
        self._root = root
        self._intake = intake
        self._bookkeeping = threading.Lock()
        self._held: dict[uuid.UUID, IntakeResult] = {}
        self._working: dict[uuid.UUID, threading.Lock] = {}
        self._released_meanwhile: set[uuid.UUID] = set()

    def folder_of(self, run_id: uuid.UUID) -> Path:
        """Where the clone of the run is or would be: for wording a failure without the path."""
        return self._root / str(run_id)

    def clone(self, run_id: uuid.UUID, source: str, tokens: TokensOfTheRun) -> IntakeResult:
        """Clone ``source`` for the run, or give the clone this worker already holds for it.

        A repeated attempt at the clone thus answers with the commit the first one read.
        ``tokens`` is called only when a clone is made. Raises ``IntakeError`` when the
        repository cannot be cloned or scanned, and whatever ``tokens`` raises; nothing of
        the run is left on the disk then.
        """
        with self._working_on(run_id):
            return self._present(run_id) or self._cloned(run_id, source, tokens, commit=None)

    def at_commit(
        self, run_id: uuid.UUID, source: str, commit: str, tokens: TokensOfTheRun
    ) -> IntakeResult:
        """The run's clone at ``commit``: the one held here, or that commit fetched again.

        ``tokens`` is called only when the commit is fetched. Raises ``IntakeError`` when the
        commit has to be fetched and cannot be, and whatever ``tokens`` raises.
        """
        with self._working_on(run_id):
            present = self._present(run_id)
            if present is not None and present.snapshot.commit_sha == commit:
                return present
            return self._cloned(run_id, source, tokens, commit=commit)

    def release(self, run_id: uuid.UUID) -> None:
        """Remove the run's clone from the disk and forget it; never raises.

        A clone that a thread is still making is removed by that thread when it is done, so
        this does not wait for it.
        """
        if self._removed_unless_at_work(run_id):
            return
        with self._bookkeeping:
            self._released_meanwhile.add(run_id)
        # The thread at work may have finished between the two lines above, and would
        # then never see the mark.
        if self._removed_unless_at_work(run_id):
            with self._bookkeeping:
                self._released_meanwhile.discard(run_id)

    def sweep(self, *, older_than: timedelta) -> int:
        """Remove what is under the root and was last written longer ago; how many it removed.

        For a worker that starts: a run whose worker died left its folder behind. A run that
        is still in progress, on a worker that shares this disk, is younger than the longest
        a run can last, which is what the caller passes.
        """
        oldest_kept = time.time() - older_than.total_seconds()
        removed = 0
        for entry in self._root.iterdir():
            try:
                if entry.lstat().st_mtime >= oldest_kept:
                    continue
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
            except OSError as e:
                logger.warning("left-over clone %s was not removed: %s", entry.name, e)
                continue
            removed += 1
        if removed:
            logger.info("removed %d left-over clones of analysis runs", removed)
        return removed

    @contextmanager
    def _working_on(self, run_id: uuid.UUID) -> Iterator[None]:
        """One thread at a time works on the clone of a run; a release that came meanwhile
        is carried out when the work is done."""
        working = self._held_lock_of(run_id)
        try:
            yield
        finally:
            with self._bookkeeping:
                released = run_id in self._released_meanwhile
                self._released_meanwhile.discard(run_id)
            if released:
                self._remove(run_id)
            working.release()

    def _held_lock_of(self, run_id: uuid.UUID) -> threading.Lock:
        """The lock of the run, held. Removing a run drops its lock, so a thread that waited
        for one that was dropped meanwhile takes the run's new one."""
        while True:
            with self._bookkeeping:
                working = self._working.setdefault(run_id, threading.Lock())
            working.acquire()
            with self._bookkeeping:
                if self._working.get(run_id) is working:
                    return working
            working.release()

    def _removed_unless_at_work(self, run_id: uuid.UUID) -> bool:
        """Remove the run's clone if no thread is working on it; whether it was removed."""
        with self._bookkeeping:
            working = self._working.setdefault(run_id, threading.Lock())
        if not working.acquire(blocking=False):
            return False
        try:
            with self._bookkeeping:
                if self._working.get(run_id) is not working:
                    # Dropped since it was looked up: another thread has the run's new lock.
                    return False
            self._remove(run_id)
        finally:
            working.release()
        return True

    def _present(self, run_id: uuid.UUID) -> IntakeResult | None:
        """What this worker holds of the run, if its folder is still on the disk."""
        with self._bookkeeping:
            held = self._held.get(run_id)
        if held is None or not held.snapshot.root.is_dir():
            return None
        return held

    def _cloned(
        self, run_id: uuid.UUID, source: str, tokens: TokensOfTheRun, *, commit: str | None
    ) -> IntakeResult:
        folder = self.folder_of(run_id)
        # What is there is not this clone: a process that died left it, or it is of another commit.
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir(mode=OWNER_ONLY)
        try:
            intake = self._intake(source, folder / REPOSITORY_FOLDER, commit, tokens())
        except BaseException:
            self._remove(run_id)
            raise
        with self._bookkeeping:
            self._held[run_id] = intake
        return intake

    def _remove(self, run_id: uuid.UUID) -> None:
        """Forget the run and remove its folder. The caller holds the run's lock."""
        shutil.rmtree(self.folder_of(run_id), ignore_errors=True)
        with self._bookkeeping:
            self._held.pop(run_id, None)
            self._working.pop(run_id, None)
