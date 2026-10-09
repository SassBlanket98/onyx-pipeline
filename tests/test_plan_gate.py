"""Plan-approval gate for code jobs."""

from __future__ import annotations

import subprocess

import pytest

from onyx_pipeline.config import load_config
from onyx_pipeline.jobs import create_job, load_job
from onyx_pipeline.stages import approve_job, revise_job, run_loop
from onyx_pipeline.wait import wait_for_job
from tests.helpers import make_git_repo


def _cfg_gated(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ONYX_AUTO_APPROVE", raising=False)
    return load_config()


def test_small_job_stops_at_needs_approval_without_build(onyx_env, monkeypatch):
    monkeypatch.setenv("STUB_CLAUDE_MODE", "ok")
    monkeypatch.setenv("STUB_REVIEW", "pass")
    cfg = _cfg_gated(monkeypatch)
    repo = make_git_repo(onyx_env.roots)
    job = create_job(cfg, kind="code", repo=str(repo), task="Add a built marker file")
    run_loop(cfg)
    job = load_job(cfg, job.id)
    assert job.state == "needs-approval"
    view = (onyx_env.jobs / job.id / "plan-view.md").read_text()
    assert "GOAL" in view
    assert "WHAT WILL CHANGE" in view
    assert job.worktree is None
    listed = subprocess.run(
        ["git", "branch", "--list", f"onyx/{job.id}"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    assert listed.stdout.strip() == ""


def test_approve_then_run_loop_reaches_done(onyx_env, monkeypatch):
    monkeypatch.setenv("STUB_CLAUDE_MODE", "ok")
    monkeypatch.setenv("STUB_REVIEW", "pass")
    cfg = _cfg_gated(monkeypatch)
    repo = make_git_repo(onyx_env.roots)
    job = create_job(cfg, kind="code", repo=str(repo), task="Add a built marker file")
    run_loop(cfg)
    approve_job(cfg, job.id)
    run_loop(cfg)
    job = load_job(cfg, job.id)
    assert job.state == "done"


def test_revise_replans_and_shows_revision(onyx_env, monkeypatch):
    monkeypatch.setenv("STUB_CLAUDE_MODE", "ok")
    monkeypatch.setenv("STUB_REVIEW", "pass")
    cfg = _cfg_gated(monkeypatch)
    repo = make_git_repo(onyx_env.roots)
    job = create_job(cfg, kind="code", repo=str(repo), task="Add a built marker file")
    run_loop(cfg)
    evidence_before = (onyx_env.jobs / job.id / "evidence.md").read_text()
    job = revise_job(cfg, job.id, "Please keep the change smaller.")
    assert job.state == "planning"
    task = (onyx_env.jobs / job.id / "task.md").read_text()
    assert "## Plan feedback (round 1)" in task
    assert "Please keep the change smaller." in task
    run_loop(cfg)
    job = load_job(cfg, job.id)
    assert job.state == "needs-approval"
    view = (onyx_env.jobs / job.id / "plan-view.md").read_text()
    assert "revision 1 of 3" in view
    assert (onyx_env.jobs / job.id / "evidence.md").read_text() == evidence_before


def test_fourth_revise_raises(onyx_env, monkeypatch):
    monkeypatch.setenv("STUB_CLAUDE_MODE", "ok")
    cfg = _cfg_gated(monkeypatch)
    repo = make_git_repo(onyx_env.roots)
    job = create_job(cfg, kind="code", repo=str(repo), task="Add a built marker file")
    run_loop(cfg)
    for n in range(3):
        revise_job(cfg, job.id, f"round {n + 1}")
        run_loop(cfg)
    with pytest.raises(ValueError, match="revision limit reached"):
        revise_job(cfg, job.id, "too many")


def test_auto_flag_skips_gate(onyx_env, monkeypatch):
    monkeypatch.setenv("STUB_CLAUDE_MODE", "ok")
    monkeypatch.setenv("STUB_REVIEW", "pass")
    cfg = _cfg_gated(monkeypatch)
    repo = make_git_repo(onyx_env.roots)
    job = create_job(cfg, kind="code", repo=str(repo), task="Add a built marker file", auto=True)
    run_loop(cfg)
    job = load_job(cfg, job.id)
    assert job.state == "done"


def test_wait_prints_plan_view_and_revise_instructions(onyx_env, monkeypatch, capsys):
    monkeypatch.setenv("STUB_CLAUDE_MODE", "ok")
    cfg = _cfg_gated(monkeypatch)
    repo = make_git_repo(onyx_env.roots)
    job = create_job(cfg, kind="code", repo=str(repo), task="Add a built marker file")
    run_loop(cfg)
    view = (onyx_env.jobs / job.id / "plan-view.md").read_text()
    assert wait_for_job(cfg, job.id, poll=0.01, sleep_fn=lambda _s: None) == 0
    out = capsys.readouterr().out
    assert view in out
    assert f"onyx-job revise {job.id}" in out
    assert "do not summarise or reword it" in out


def test_cli_revise_reports_error_cleanly(onyx_env, monkeypatch, capsys) -> None:
    from onyx_pipeline.cli import main

    repo = make_git_repo(onyx_env.roots)
    job = create_job(onyx_env.cfg, kind="code", repo=str(repo), task="Not waiting for approval")
    with pytest.raises(SystemExit) as exc:
        main(["revise", job.id, "change it"])
    assert exc.value.code == 1
    assert "not waiting for approval" in capsys.readouterr().err
