"""Tests for the clones of runs on a worker's disk.

Cloning is scripted (``ScriptedIntake`` in ``conftest.py`` of the worker's tests writes a small
repository), except where a test says real git: those clone a repository on this machine and
need gitleaks. Nothing here needs a database or a Temporal server.
"""

import os
import stat
import subprocess
import threading
import time
import uuid
from datetime import timedelta
from pathlib import Path

import pytest

from openmarketer_core.repository_analysis.intake import IntakeError, IntakeResult
from openmarketer_worker.repository_analyzer.clones import LocalClones

SOURCE = "https://example.com/acme/app.git"
OTHER_COMMIT = "b" * 40
A_DAY = timedelta(days=1)


@pytest.fixture
def clones(clones_root, intake) -> LocalClones:
    return LocalClones(clones_root, intake)


@pytest.fixture
def run_id() -> uuid.UUID:
    return uuid.uuid4()


def permissions(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def aged(path: Path, age: timedelta) -> Path:
    """``path``, last written ``age`` ago."""
    then = time.time() - age.total_seconds()
    os.utime(path, (then, then))
    return path


# ------------------------------------------------------------------ the folders
def test_clone_is_made_in_a_folder_named_by_the_id_of_the_run_and_nothing_else(
    clones, clones_root, intake, run_id
):
    clones.clone(run_id, SOURCE)
    ((_, cloned_into, _),) = intake.cloned
    assert cloned_into.parent == clones_root / str(run_id)
    assert [entry.name for entry in clones_root.iterdir()] == [str(run_id)]


def test_root_and_the_folder_of_a_run_are_for_the_workers_user_only(clones, clones_root, run_id):
    clones.clone(run_id, SOURCE)
    assert permissions(clones_root) == 0o700
    assert permissions(clones.folder_of(run_id)) == 0o700


def test_root_that_was_open_to_others_is_closed_to_them(clones_root, intake):
    clones_root.mkdir(mode=0o755)
    LocalClones(clones_root, intake)
    assert permissions(clones_root) == 0o700


def test_root_that_is_a_link_to_somewhere_else_is_refused(tmp_path, intake):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    link = tmp_path / "link"
    link.symlink_to(elsewhere)
    with pytest.raises(PermissionError, match="not a folder of the user"):
        LocalClones(link, intake)


def test_root_that_cannot_be_a_folder_is_an_error_of_the_system(tmp_path, intake):
    taken = tmp_path / "taken"
    taken.write_text("a file")
    with pytest.raises(OSError):
        LocalClones(taken, intake)


# -------------------------------------------------------------------- cloning
def test_clone_is_of_the_default_branch_of_the_source(clones, intake, run_id):
    cloned = clones.clone(run_id, SOURCE)
    assert [(source, commit) for source, _, commit in intake.cloned] == [(SOURCE, None)]
    assert cloned.files.read_text("README.md").startswith("# Example App")


def test_repeated_clone_of_a_run_gives_the_clone_it_has(clones, intake, run_id):
    first = clones.clone(run_id, SOURCE)
    again = clones.clone(run_id, SOURCE)
    assert again is first
    assert len(intake.cloned) == 1


def test_runs_have_clones_of_their_own(clones, intake):
    first, second = uuid.uuid4(), uuid.uuid4()
    clones.clone(first, SOURCE)
    clones.clone(second, SOURCE)
    assert clones.folder_of(first) != clones.folder_of(second)
    assert len(intake.cloned) == 2


def test_clone_that_fails_leaves_nothing_of_the_run_on_the_disk(
    clones, clones_root, intake, run_id
):
    intake.failures.append(IntakeError("the repository was not found"))
    with pytest.raises(IntakeError, match="was not found"):
        clones.clone(run_id, SOURCE)
    assert list(clones_root.iterdir()) == []


def test_clone_after_one_that_failed_is_made(clones, intake, run_id):
    intake.failures.append(IntakeError("the repository host could not be reached"))
    with pytest.raises(IntakeError):
        clones.clone(run_id, SOURCE)
    assert clones.clone(run_id, SOURCE).snapshot.root.is_dir()


def test_folder_a_dead_process_left_for_the_run_is_replaced(clones, clones_root, run_id):
    left_behind = clones_root / str(run_id) / "repo"
    left_behind.mkdir(parents=True)
    (left_behind / "half-a-clone").write_text("x")
    cloned = clones.clone(run_id, SOURCE)
    assert sorted(path.name for path in cloned.snapshot.root.iterdir()) == ["README.md"]


def test_two_threads_that_clone_for_one_run_clone_once(clones_root, intake, run_id):
    both_asked = threading.Barrier(2, timeout=10)

    def slow_intake(source: str, clone_into: Path, commit: str | None) -> IntakeResult:
        time.sleep(0.2)
        return intake(source, clone_into, commit)

    clones = LocalClones(clones_root, slow_intake)
    results: list[IntakeResult] = []

    def clone() -> None:
        both_asked.wait()
        results.append(clones.clone(run_id, SOURCE))

    threads = [threading.Thread(target=clone) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert len(intake.cloned) == 1
    assert results[0] is results[1]


# ----------------------------------------------------------------- at a commit
def test_clone_that_is_held_at_the_commit_is_given_without_cloning(clones, intake, run_id):
    held = clones.clone(run_id, SOURCE)
    assert clones.at_commit(run_id, SOURCE, held.snapshot.commit_sha) is held
    assert len(intake.cloned) == 1


def test_clone_whose_folder_is_gone_is_made_again_at_the_same_commit(clones, intake, run_id):
    first = clones.clone(run_id, SOURCE)
    commit = first.snapshot.commit_sha
    clones.release(run_id)
    again = clones.at_commit(run_id, SOURCE, commit)
    assert [asked for _, _, asked in intake.cloned] == [None, commit]
    assert again.snapshot.commit_sha == commit
    assert again.snapshot.root.is_dir()


def test_worker_that_never_had_the_clone_makes_it_at_the_commit_of_the_run(
    tmp_path, intake, run_id
):
    another_worker = LocalClones(tmp_path / "another-disk", intake)
    cloned = another_worker.at_commit(run_id, SOURCE, OTHER_COMMIT)
    assert [asked for _, _, asked in intake.cloned] == [OTHER_COMMIT]
    assert cloned.snapshot.commit_sha == OTHER_COMMIT


def test_clone_held_at_another_commit_is_replaced_by_the_commit_asked_for(clones, intake, run_id):
    clones.clone(run_id, SOURCE)
    cloned = clones.at_commit(run_id, SOURCE, OTHER_COMMIT)
    assert cloned.snapshot.commit_sha == OTHER_COMMIT
    assert clones.at_commit(run_id, SOURCE, OTHER_COMMIT) is cloned


def test_commit_that_cannot_be_fetched_again_leaves_nothing_on_the_disk(
    clones, clones_root, intake, run_id
):
    intake.failures.append(IntakeError("the requested commit could not be fetched"))
    with pytest.raises(IntakeError):
        clones.at_commit(run_id, SOURCE, OTHER_COMMIT)
    assert list(clones_root.iterdir()) == []


def test_real_git_fetches_the_commit_of_the_run_after_the_branch_has_moved(
    clones_root, source_repo, intake_of_a_local_repository, run_id
):
    clones = LocalClones(clones_root, intake_of_a_local_repository)
    first = clones.clone(run_id, SOURCE)
    clones.release(run_id)
    (source_repo / "README.md").write_text("# Renamed App\n")
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qam", "second"],
        cwd=source_repo,
        check=True,
        capture_output=True,
    )
    again = clones.at_commit(run_id, SOURCE, first.snapshot.commit_sha)
    assert again.snapshot.commit_sha == first.snapshot.commit_sha
    assert again.files.read_text("README.md").startswith("# Example App")


# ------------------------------------------------------------------- releasing
def test_released_run_has_no_folder_and_is_cloned_anew_when_asked_again(clones, intake, run_id):
    clones.clone(run_id, SOURCE)
    clones.release(run_id)
    assert not clones.folder_of(run_id).exists()
    clones.clone(run_id, SOURCE)
    assert len(intake.cloned) == 2


def test_releasing_leaves_the_clone_of_another_run(clones):
    released, kept = uuid.uuid4(), uuid.uuid4()
    clones.clone(released, SOURCE)
    clones.clone(kept, SOURCE)
    clones.release(released)
    assert clones.folder_of(kept).is_dir()


def test_releasing_a_run_that_has_no_clone_or_twice_does_nothing(clones, run_id):
    clones.release(run_id)
    clones.clone(run_id, SOURCE)
    clones.release(run_id)
    clones.release(run_id)
    assert not clones.folder_of(run_id).exists()


def test_run_released_while_its_clone_is_being_made_is_not_waited_for_and_leaves_nothing(
    clones_root, intake, run_id
):
    cloning = threading.Event()
    go_on = threading.Event()

    def held_intake(source: str, clone_into: Path, commit: str | None) -> IntakeResult:
        cloning.set()
        assert go_on.wait(timeout=10)
        return intake(source, clone_into, commit)

    clones = LocalClones(clones_root, held_intake)
    making = threading.Thread(target=clones.clone, args=(run_id, SOURCE))
    making.start()
    try:
        assert cloning.wait(timeout=10)
        clones.release(run_id)  # returns although the clone is not made yet
    finally:
        go_on.set()
        making.join(timeout=10)
    assert not clones.folder_of(run_id).exists()


# -------------------------------------------------------------------- the sweep
def test_sweep_removes_a_folder_older_than_a_run_can_last(clones, clones_root):
    left_behind = clones_root / str(uuid.uuid4())
    (left_behind / "repo").mkdir(parents=True)
    aged(left_behind, timedelta(days=2))
    assert clones.sweep(older_than=A_DAY) == 1
    assert not left_behind.exists()


def test_sweep_leaves_the_folder_of_a_run_that_can_still_be_in_progress(clones, run_id):
    clones.clone(run_id, SOURCE)
    aged(clones.folder_of(run_id), timedelta(hours=23))
    assert clones.sweep(older_than=A_DAY) == 0
    assert clones.at_commit(run_id, SOURCE, "a" * 40).snapshot.root.is_dir()


def test_sweep_removes_a_link_without_following_it(clones, clones_root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "kept.txt").write_text("not ours")
    link = clones_root / "a-link"
    link.symlink_to(outside)
    then = time.time() - timedelta(days=2).total_seconds()
    os.utime(link, (then, then), follow_symlinks=False)
    assert clones.sweep(older_than=A_DAY) == 1
    assert (outside / "kept.txt").exists()


def test_sweep_of_an_empty_root_removes_nothing(clones):
    assert clones.sweep(older_than=A_DAY) == 0
