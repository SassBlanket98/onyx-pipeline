"""Regression tests for the Codex review findings on the pipeline."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from onyx_pipeline.jobs import create_job, load_job, save_job
from onyx_pipeline.stages import approve_job, commit_if_changes, run_loop
from onyx_pipeline.workers import kill_active_workers


def test_approve_rejected_before_the_gate(onyx_env) -> None:
    job = create_job(onyx_env.cfg, kind="research", task="Look it up", repo=None)
    with pytest.raises(ValueError, match="not waiting for approval"):
        approve_job(onyx_env.cfg, job.id)
    assert not load_job(onyx_env.cfg, job.id).flags.get("approved")


def test_approve_accepted_at_the_gate(onyx_env) -> None:
    job = create_job(onyx_env.cfg, kind="research", task="Look it up", repo=None)
    job.state = "needs-approval"
    save_job(onyx_env.cfg, job)
    assert approve_job(onyx_env.cfg, job.id).flags["approved"] is True


def test_junk_only_change_does_not_fail_commit(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "a.py").write_text("x = 1\n")
    commit_if_changes(tmp_path, "seed")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "a.pyc").write_bytes(b"\0")
    commit_if_changes(tmp_path, "junk only")  # must not raise
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=tmp_path, check=True, capture_output=True, text=True
    )
    assert len(log.stdout.splitlines()) == 1


def test_cancel_kills_running_worker_group(tmp_path: Path) -> None:
    proc = subprocess.Popen(["bash", "-c", "sleep 300 & sleep 300"], start_new_session=True)
    (tmp_path / "build.log.pgid").write_text(str(proc.pid))
    kill_active_workers(tmp_path)
    proc.wait(timeout=10)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.killpg(proc.pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        os.killpg(proc.pid, signal.SIGKILL)
        pytest.fail("worker process group survived cancel")
    assert not (tmp_path / "build.log.pgid").exists()


def test_research_fails_when_fact_check_breaks(onyx_env, monkeypatch) -> None:
    monkeypatch.setenv("STUB_CLAUDE_MODE", "ok")
    monkeypatch.setenv("STUB_REVIEW", "broken")
    job = create_job(onyx_env.cfg, kind="research", task="Compare two things", repo=None)
    run_loop(onyx_env.cfg)
    job = load_job(onyx_env.cfg, job.id)
    assert job.state == "failed"
    assert "fact-check failed" in (job.error or "")
