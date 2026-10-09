"""Split-plan parts, job worktrees, and fallback behaviour."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from onyx_pipeline.jobs import create_job, load_job
from onyx_pipeline.stages import run_loop
from tests.helpers import make_git_repo, seed_files


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def _show(repo: Path, branch: str, rel: str) -> str:
    return _git(repo, "show", f"{branch}:{rel}")


def _times(path: Path) -> dict[int, dict[str, float]]:
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return {int(row["n"]): row for row in rows}


def _part_repo(
    onyx_env,
    monkeypatch,
    mode: str,
    files: list[str],
    extra: dict[str, str] | None = None,
):
    monkeypatch.setenv("STUB_CLAUDE_MODE", mode)
    monkeypatch.setenv("STUB_REVIEW", "pass")
    repo = make_git_repo(onyx_env.roots, name=mode.replace("-", "_"))
    seed_files(repo, files, extra=extra)
    return repo


def test_three_independent_parts_build_concurrently(onyx_env, monkeypatch, tmp_path):
    times_file = tmp_path / "times.jsonl"
    monkeypatch.setenv("STUB_TIMES_FILE", str(times_file))
    monkeypatch.setenv("STUB_BUILD_SLEEP", "0.4")
    repo = _part_repo(onyx_env, monkeypatch, "parts", ["a.py", "b.py", "c.py"])
    job = create_job(onyx_env.cfg, kind="code", repo=str(repo), task="Split into three files")
    run_loop(onyx_env.cfg)
    job = load_job(onyx_env.cfg, job.id)
    assert job.state == "done"
    assert "part 1" in _show(repo, job.branch, "a.py")
    assert "part 2" in _show(repo, job.branch, "b.py")
    assert "part 3" in _show(repo, job.branch, "c.py")
    log = _git(repo, "log", "--oneline", f"{job.base_branch}..{job.branch}")
    assert log.count("merge part") == 3
    by_n = _times(times_file)
    assert set(by_n) == {1, 2, 3}
    spans = list(by_n.values())
    overlapping = 0
    for i, left in enumerate(spans):
        for right in spans[i + 1 :]:
            if left["start"] < right["end"] and right["start"] < left["end"]:
                overlapping += 1
    assert overlapping >= 1
    listed = _git(repo, "branch", "--list", f"{job.branch}-p*")
    assert listed.strip() == ""
    parts_dir = onyx_env.jobs / job.id / "parts"
    assert not parts_dir.exists() or not any(parts_dir.iterdir())
    summary = (onyx_env.jobs / job.id / "summary.md").read_text()
    assert "Built in 3 parts" in summary


def test_dependent_part_starts_after_merge(onyx_env, monkeypatch, tmp_path):
    times_file = tmp_path / "times.jsonl"
    monkeypatch.setenv("STUB_TIMES_FILE", str(times_file))
    monkeypatch.setenv("STUB_BUILD_SLEEP", "0.2")
    repo = _part_repo(onyx_env, monkeypatch, "parts-dep", ["a.py", "b.py"])
    job = create_job(onyx_env.cfg, kind="code", repo=str(repo), task="Dependent second part")
    run_loop(onyx_env.cfg)
    job = load_job(onyx_env.cfg, job.id)
    assert job.state == "done"
    by_n = _times(times_file)
    assert by_n[2]["start"] >= by_n[1]["end"]
    assert "part 1" in _show(repo, job.branch, "a.py")
    assert "part 2" in _show(repo, job.branch, "b.py")
    log = _git(repo, "log", "--oneline", f"{job.base_branch}..{job.branch}")
    assert "merge part 1" in log
    assert "part 2" in log


def test_merge_clash_is_rebuilt(onyx_env, monkeypatch):
    repo = _part_repo(
        onyx_env,
        monkeypatch,
        "parts-clash",
        ["a.py", "b.py"],
        extra={"shared.py": "x = 0\n"},
    )
    job = create_job(onyx_env.cfg, kind="code", repo=str(repo), task="Clash then rebuild")
    run_loop(onyx_env.cfg)
    job = load_job(onyx_env.cfg, job.id)
    assert job.state == "done"
    states = {int(p["n"]): p["state"] for p in job.parts}
    assert "rebuilt" in states.values()
    summary = (onyx_env.jobs / job.id / "summary.md").read_text()
    assert "rebuilt after a merge clash" in summary
    assert "part 1" in _show(repo, job.branch, "a.py")
    assert "part 2" in _show(repo, job.branch, "b.py")


@pytest.mark.parametrize(
    "mode,needle",
    [
        ("parts-bad", "malformed"),
        ("parts-too-many", "more"),
        ("parts-cycle", "cycle"),
        ("parts-shared", "file"),
    ],
)
def test_bad_parts_falls_back_to_one(onyx_env, monkeypatch, mode, needle):
    repo = _part_repo(
        onyx_env,
        monkeypatch,
        mode,
        ["a.py", "b.py", "p1.py", "p2.py", "p3.py", "p4.py", "shared.py"],
    )
    job = create_job(onyx_env.cfg, kind="code", repo=str(repo), task=f"Fallback for {mode}")
    run_loop(onyx_env.cfg)
    job = load_job(onyx_env.cfg, job.id)
    assert job.state == "done"
    assert job.split_note
    assert needle in job.split_note.lower()
    assert job.parts == []
    log = _git(repo, "log", "--oneline", f"{job.base_branch}..{job.branch}")
    assert "building" in log
    summary = (onyx_env.jobs / job.id / "summary.md").read_text()
    # The malformed-heading note says nothing once the job is done, so the summary drops it.
    assert (job.split_note in summary) is (job.split_note != "malformed PARTS section")


def test_no_parts_is_todays_single_build(onyx_env, monkeypatch):
    monkeypatch.setenv("STUB_CLAUDE_MODE", "ok")
    repo = make_git_repo(onyx_env.roots, name="single")
    job = create_job(onyx_env.cfg, kind="code", repo=str(repo), task="Single build as today")
    run_loop(onyx_env.cfg)
    job = load_job(onyx_env.cfg, job.id)
    assert job.state == "done"
    assert job.split_note is None
    assert job.parts == []
    log = _git(repo, "log", "--oneline", f"{job.base_branch}..{job.branch}")
    assert f"onyx-job {job.id}: building" in log
    summary = (onyx_env.jobs / job.id / "summary.md").read_text()
    assert "Built in" not in summary


def test_worktree_removed_on_done_kept_on_failed(onyx_env, monkeypatch):
    monkeypatch.setenv("STUB_REVIEW", "pass")
    repo = make_git_repo(onyx_env.roots, name="wt_done")
    job = create_job(onyx_env.cfg, kind="code", repo=str(repo), task="Worktree gone when done")
    run_loop(onyx_env.cfg)
    job = load_job(onyx_env.cfg, job.id)
    assert job.state == "done"
    assert job.worktree
    assert not Path(job.worktree).exists()
    _git(repo, "rev-parse", "--verify", job.branch)

    monkeypatch.setenv("STUB_REVIEW", "fail")
    repo2 = make_git_repo(onyx_env.roots, name="wt_fail")
    job2 = create_job(onyx_env.cfg, kind="code", repo=str(repo2), task="Worktree kept when failed")
    run_loop(onyx_env.cfg)
    job2 = load_job(onyx_env.cfg, job2.id)
    assert job2.state == "failed"
    assert job2.worktree
    assert Path(job2.worktree).exists()
    summary = (onyx_env.jobs / job2.id / "summary.md").read_text()
    assert job2.worktree in summary


def test_part_worker_failure_fails_job_naming_part(onyx_env, monkeypatch, tmp_path):
    times_file = tmp_path / "times.jsonl"
    monkeypatch.setenv("STUB_TIMES_FILE", str(times_file))
    monkeypatch.setenv("STUB_BUILD_SLEEP", "0.3")
    monkeypatch.setenv("STUB_FAIL_PART", "2")
    repo = _part_repo(onyx_env, monkeypatch, "parts", ["a.py", "b.py", "c.py"])
    job = create_job(onyx_env.cfg, kind="code", repo=str(repo), task="One part worker dies")
    run_loop(onyx_env.cfg)
    job = load_job(onyx_env.cfg, job.id)
    assert job.state == "failed"
    assert job.error
    assert "part 2" in job.error
    by_n = _times(times_file)
    assert 1 in by_n and 2 in by_n
    assert Path(job.worktree).exists()
    parts_dir = onyx_env.jobs / job.id / "parts"
    assert not parts_dir.exists() or not any(parts_dir.iterdir())
    listed = _git(repo, "branch", "--list", f"{job.branch}-p*")
    assert listed.strip() == ""


def test_malformed_parts_note_hidden_once_done():
    from types import SimpleNamespace

    from onyx_pipeline.summary import _parts_line

    note = "malformed PARTS section"
    assert _parts_line(SimpleNamespace(split_note=note, state="done", parts=[])) is None
    assert _parts_line(SimpleNamespace(split_note=note, state="failed", parts=[])) == note
    other = "too many parts"
    assert _parts_line(SimpleNamespace(split_note=other, state="done", parts=[])) == other
