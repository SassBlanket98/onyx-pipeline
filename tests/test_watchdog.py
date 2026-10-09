"""Watchdog around build/fix workers."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from onyx_pipeline.jobs import create_job, job_dir
from onyx_pipeline.stages import JobFailed, _build_watched
from onyx_pipeline.workers import STALLED, Watch, changed_paths, run_capture
from tests.helpers import make_git_repo


def _watch(repo: Path, idle_limit: int = 1, poll: float = 0.2, on_tick=None) -> Watch:
    base = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    return Watch(repo=repo, base=base, idle_limit=idle_limit, poll=poll, on_tick=on_tick)


def test_watchdog_kills_idle_worker(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path)
    watch = _watch(repo, idle_limit=1, poll=0.2)
    started = time.monotonic()
    code, _out, err = run_capture(
        ["bash", "-c", "sleep 30"],
        cwd=repo,
        timeout=300,
        watch=watch,
    )
    assert time.monotonic() - started < 5
    assert code == STALLED
    assert "killed by the watchdog" in err


def test_watchdog_allows_file_activity(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path)
    ticks: list[list[str]] = []

    def on_tick(paths: list[str]) -> None:
        ticks.append(list(paths))

    watch = _watch(repo, idle_limit=1, poll=0.2, on_tick=on_tick)
    script = "for i in $(seq 1 10); do echo x >> touchme.txt; sleep 0.3; done"
    code, _out, _err = run_capture(
        ["bash", "-c", script],
        cwd=repo,
        timeout=30,
        watch=watch,
    )
    assert code == 0
    assert any("touchme.txt" in t for t in ticks)


def test_watchdog_counts_output_as_activity(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path)
    watch = _watch(repo, idle_limit=1, poll=0.2)
    script = "for i in $(seq 1 10); do echo line; sleep 0.3; done"
    code, out, _err = run_capture(
        ["bash", "-c", script],
        cwd=repo,
        timeout=30,
        watch=watch,
    )
    assert code == 0
    assert "line" in out


def test_watchdog_stalled_after_one_file(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path)
    base = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    watch = _watch(repo, idle_limit=1, poll=0.2)
    code, _out, err = run_capture(
        ["bash", "-c", "echo hi > once.txt; sleep 30"],
        cwd=repo,
        timeout=300,
        watch=watch,
    )
    assert code == STALLED
    assert "killed by the watchdog" in err
    assert "once.txt" in changed_paths(repo, base)


def test_run_capture_without_watch(tmp_path: Path) -> None:
    code, out, _err = run_capture(
        ["bash", "-c", "echo hello"],
        cwd=tmp_path,
        timeout=10,
    )
    assert code == 0
    assert out.strip() == "hello"


def test_changed_paths_git_cases(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path)
    base = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    (repo / "new.txt").write_text("n\n")
    assert "new.txt" in changed_paths(repo, base)
    (repo / "README").write_text("changed\n")
    assert "README" in changed_paths(repo, base)
    (repo / "oldname").write_text("x\n")
    subprocess.run(["git", "add", "oldname"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "old"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "mv", "oldname", "newname"], cwd=repo, check=True, capture_output=True)
    paths = changed_paths(repo, base)
    assert "newname" in paths
    assert changed_paths(Path("/not/a/repo"), base) == []


def test_build_watched_stall_with_changes(monkeypatch, onyx_env) -> None:
    cfg = onyx_env.cfg
    job = create_job(cfg, kind="code", task="t", repo=str(onyx_env.roots / "app"))
    directory = job_dir(cfg, job.id)
    repo = make_git_repo(onyx_env.roots, name="app")

    def fake_build(*_a, **_k):
        return STALLED, ""

    monkeypatch.setattr("onyx_pipeline.stages.build", fake_build)
    monkeypatch.setattr("onyx_pipeline.stages.changed_paths", lambda _r, _b: ["a.py"])
    code = _build_watched(cfg, job, directory, repo, "m", "brief")
    assert code == 0
    assert job.flags["stalls"][-1]["action"] == "continued"


def test_build_watched_retries_composer(monkeypatch, onyx_env) -> None:
    from onyx_pipeline.workers import COMPOSER_MODEL, GROK_MODEL

    cfg = onyx_env.cfg
    job = create_job(cfg, kind="code", task="t", repo=str(onyx_env.roots / "app2"))
    directory = job_dir(cfg, job.id)
    repo = make_git_repo(onyx_env.roots, name="app2")
    calls: list[str] = []

    def fake_build(_cfg, _d, _r, model, *_a, **_k):
        calls.append(model)
        return STALLED, ""

    monkeypatch.setattr("onyx_pipeline.stages.build", fake_build)
    monkeypatch.setattr("onyx_pipeline.stages.changed_paths", lambda _r, _b: [])
    with pytest.raises(JobFailed, match="twice"):
        _build_watched(cfg, job, directory, repo, GROK_MODEL, "brief")
    assert calls == [GROK_MODEL, COMPOSER_MODEL]
    assert job.flags["stalls"][0]["action"] == "retried on composer"


def test_build_watched_stall_twice_no_changes(monkeypatch, onyx_env) -> None:
    from onyx_pipeline.workers import COMPOSER_MODEL

    cfg = onyx_env.cfg
    job = create_job(cfg, kind="code", task="t", repo=str(onyx_env.roots / "app3"))
    directory = job_dir(cfg, job.id)
    repo = make_git_repo(onyx_env.roots, name="app3")

    monkeypatch.setattr("onyx_pipeline.stages.build", lambda *_a, **_k: (STALLED, ""))
    monkeypatch.setattr("onyx_pipeline.stages.changed_paths", lambda _r, _b: [])
    with pytest.raises(JobFailed, match="once"):
        _build_watched(cfg, job, directory, repo, COMPOSER_MODEL, "brief")


def test_build_watched_normal_exit(monkeypatch, onyx_env) -> None:
    cfg = onyx_env.cfg
    job = create_job(cfg, kind="code", task="t", repo=str(onyx_env.roots / "app4"))
    directory = job_dir(cfg, job.id)
    repo = make_git_repo(onyx_env.roots, name="app4")
    calls = 0

    def fake_build(*_a, **_k):
        nonlocal calls
        calls += 1
        return 2, ""

    monkeypatch.setattr("onyx_pipeline.stages.build", fake_build)
    assert _build_watched(cfg, job, directory, repo, "m", "brief") == 2
    assert calls == 1


def test_changed_paths_lists_files_inside_new_directory(tmp_path):
    repo = make_git_repo(tmp_path)
    (repo / "pkg").mkdir()
    (repo / "pkg" / "a.py").write_text("x = 1\n")
    (repo / "pkg" / "b.py").write_text("y = 2\n")
    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert changed_paths(repo, base) == ["pkg/a.py", "pkg/b.py"]
