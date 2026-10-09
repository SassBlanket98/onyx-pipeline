"""Git safety: junk never committed, branch switches and history rewrites fail the job."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from onyx_pipeline.stages import JobFailed, commit_if_changes, guard_branch


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "a.py").write_text("x = 1\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "seed")
    _git(tmp_path, "checkout", "-qb", "onyx/job")
    return tmp_path


class _Job:
    branch = "onyx/job"


def test_pycache_is_never_committed(repo: Path) -> None:
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "a.cpython-312.pyc").write_bytes(b"\0")
    (repo / "b.py").write_text("y = 2\n")
    commit_if_changes(repo, "work")
    files = _git(repo, "show", "--name-only", "--format=", "HEAD").split()
    assert files == ["b.py"]


def test_branch_switch_fails(repo: Path) -> None:
    before = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-qb", "feat/other")
    with pytest.raises(JobFailed, match="switched branch"):
        guard_branch(repo, _Job(), before)  # type: ignore[arg-type]


def test_history_rewrite_fails(repo: Path) -> None:
    (repo / "b.py").write_text("y = 2\n")
    commit_if_changes(repo, "work")
    before = _git(repo, "rev-parse", "HEAD")
    _git(repo, "commit", "-q", "--amend", "-m", "rewritten")
    with pytest.raises(JobFailed, match="rewrote git history"):
        guard_branch(repo, _Job(), before)  # type: ignore[arg-type]


def test_normal_commit_passes(repo: Path) -> None:
    before = _git(repo, "rev-parse", "HEAD")
    (repo / "c.py").write_text("z = 3\n")
    commit_if_changes(repo, "more")
    guard_branch(repo, _Job(), before)  # type: ignore[arg-type]


def test_verify_commands_drop_cd_and_point_at_worktree(tmp_path: Path) -> None:
    from onyx_pipeline.stages import verify_commands

    class J:
        repo = str(tmp_path / "repo")
        worktree = str(tmp_path / "jobs" / "x" / "worktree")

    line = f"cd {J.repo} && uv run pytest -q && python {J.repo}/tool.py --check"
    assert verify_commands(line, J()) == [  # type: ignore[arg-type]
        "uv run pytest -q",
        f"python {J.worktree}/tool.py --check",
    ]
