"""Ops job contract: CLI entry, bypass-flag rejection, states, flags, queued."""

from __future__ import annotations

import pytest

from onyx_pipeline import stages
from onyx_pipeline.cli import run
from onyx_pipeline.jobs import (
    DEFAULT_FLAGS,
    STATES,
    create_job,
    is_runnable,
    list_jobs,
    load_job,
    save_job,
)


def test_states_include_briefing_and_operating():
    assert "briefing" in STATES
    assert "operating" in STATES


def test_default_flags_include_ops_fields():
    assert "ops_brief_sha256" in DEFAULT_FLAGS
    assert "ops_changes" in DEFAULT_FLAGS
    assert "ops_approval_message_id" in DEFAULT_FLAGS


def test_start_ops_preserves_task_verbatim(onyx_env, monkeypatch, capsys):
    monkeypatch.setattr("onyx_pipeline.cli.spawn_runner", lambda cfg: None)
    raw = "Draft an e-mail — café ☕\nsecond line\t--dry-run"

    assert run(["start", "--kind", "ops", "--", raw]) == 0
    job_id = capsys.readouterr().out.strip()

    job = load_job(onyx_env.cfg, job_id)
    assert job.kind == "ops"
    assert job.repo is None
    assert job.state == "queued"
    assert job.task == raw
    assert (onyx_env.jobs / job_id / "task.md").read_text() == f"# Task\n\n{raw}\n"


def test_start_ops_accepts_leading_dash_task(onyx_env, monkeypatch, capsys):
    monkeypatch.setattr("onyx_pipeline.cli.spawn_runner", lambda cfg: None)
    raw = "--restart nginx now"

    assert run(["start", "--kind", "ops", "--", raw]) == 0
    job_id = capsys.readouterr().out.strip()
    assert load_job(onyx_env.cfg, job_id).task == raw


def test_start_ops_request_id_is_idempotent(onyx_env, monkeypatch, capsys):
    spawns: list[int] = []
    monkeypatch.setattr("onyx_pipeline.cli.spawn_runner", lambda cfg: spawns.append(1))
    argv = ["start", "--kind", "ops", "--request-id", "req_op1", "--", "restart the web server"]

    assert run(argv) == 0
    first = capsys.readouterr().out.strip()
    assert run(argv) == 0
    second = capsys.readouterr().out.strip()

    assert first
    assert first == second
    assert len(spawns) == 1
    assert [job.id for job in list_jobs(onyx_env.cfg)] == [first]
    assert load_job(onyx_env.cfg, first).request_id == "req_op1"


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        (["--repo", "/tmp/somewhere"], "--repo"),
        (["--auto"], "--auto"),
        (["--auto-quote", "do it"], "--auto-quote"),
        (["--deep"], "--deep"),
        (["--quick"], "--quick"),
    ],
)
def test_ops_rejects_bypass_flags(onyx_env, capsys, extra, expected):
    code = run(["start", "--kind", "ops", *extra, "--", "do the thing"])

    assert code == 2
    err = capsys.readouterr().err
    assert expected in err
    assert "--kind ops" in err
    assert list_jobs(onyx_env.cfg) == []


def test_ops_rejection_lists_every_bad_flag(onyx_env, capsys):
    code = run(["start", "--kind", "ops", "--auto", "--deep", "--", "do the thing"])

    assert code == 2
    err = capsys.readouterr().err
    assert "--auto" in err
    assert "--deep" in err
    assert list_jobs(onyx_env.cfg) == []


def test_ops_job_state_round_trips(onyx_env):
    job = create_job(onyx_env.cfg, kind="ops", task="check the certificate", repo=None)
    job.flags["ops_brief_sha256"] = "abc123"
    job.flags["ops_changes"] = ["restart nginx", "renew cert"]
    job.flags["ops_approval_message_id"] = 42
    save_job(onyx_env.cfg, job)

    loaded = load_job(onyx_env.cfg, job.id)
    assert loaded.kind == "ops"
    assert loaded.state == "queued"
    assert loaded.flags["ops_brief_sha256"] == "abc123"
    assert loaded.flags["ops_changes"] == ["restart nginx", "renew cert"]
    assert loaded.flags["ops_approval_message_id"] == 42


def test_ops_job_is_runnable_in_queued(onyx_env):
    job = create_job(onyx_env.cfg, kind="ops", task="restart the web server", repo=None)
    assert is_runnable(job) is True


def test_process_job_routes_ops_to_process_ops(onyx_env, monkeypatch):
    called: list[str] = []
    monkeypatch.setattr(stages, "process_ops", lambda cfg, job: called.append(job.id))
    job = create_job(onyx_env.cfg, kind="ops", task="restart the web server", repo=None)

    stages.process_job(onyx_env.cfg, job)

    assert called == [job.id]
    assert load_job(onyx_env.cfg, job.id).runner_pid is None
