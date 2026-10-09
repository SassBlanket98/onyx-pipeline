"""CLI guards: duplicates, cancel confirm, auto quote, id-less gate commands."""

from __future__ import annotations

from onyx_pipeline.cli import run
from onyx_pipeline.config import load_config
from onyx_pipeline.jobs import create_job, load_job, save_job
from onyx_pipeline.stages import resolve_waiting_job


def test_duplicate_refused(onyx_env, monkeypatch, capsys) -> None:
    monkeypatch.setattr("onyx_pipeline.cli.spawn_runner", lambda _cfg: None)
    monkeypatch.setenv("ONYX_DUP_WINDOW_MIN", "30")
    cfg = load_config()
    repo = onyx_env.roots / "dup-app"
    repo.mkdir()
    create_job(cfg, kind="code", task="Add logging to the widget service", repo=str(repo))
    code = run(
        [
            "start",
            "--kind",
            "code",
            "--repo",
            str(repo),
            "Add logging to the widget service for production",
        ]
    )
    assert code == 2
    assert "similar job" in capsys.readouterr().err


def test_duplicate_different_task_allowed(onyx_env, monkeypatch, capsys) -> None:
    monkeypatch.setattr("onyx_pipeline.cli.spawn_runner", lambda _cfg: None)
    monkeypatch.setenv("ONYX_DUP_WINDOW_MIN", "30")
    cfg = load_config()
    repo = onyx_env.roots / "dup-app2"
    repo.mkdir()
    create_job(cfg, kind="code", task="Refactor authentication module", repo=str(repo))
    code = run(
        [
            "start",
            "--kind",
            "code",
            "--repo",
            str(repo),
            "Write comprehensive unit tests for payments",
        ]
    )
    assert code == 0
    assert capsys.readouterr().out.strip()


def test_allow_duplicate_override(onyx_env, monkeypatch, capsys) -> None:
    monkeypatch.setattr("onyx_pipeline.cli.spawn_runner", lambda _cfg: None)
    monkeypatch.setenv("ONYX_DUP_WINDOW_MIN", "30")
    cfg = load_config()
    repo = onyx_env.roots / "dup-app3"
    repo.mkdir()
    create_job(cfg, kind="code", task="Add logging to the widget service", repo=str(repo))
    code = run(
        [
            "start",
            "--kind",
            "code",
            "--repo",
            str(repo),
            "--allow-duplicate",
            "Add logging to the widget service for production",
        ]
    )
    assert code == 0


def test_cancel_without_confirm(onyx_env, capsys) -> None:
    job = create_job(onyx_env.cfg, kind="research", task="x", repo=None)
    code = run(["cancel", job.id])
    assert code == 2
    assert "--confirm" in capsys.readouterr().err


def test_cancel_with_confirm(onyx_env) -> None:
    job = create_job(onyx_env.cfg, kind="research", task="x", repo=None)
    code = run(["cancel", job.id, "--confirm"])
    assert code == 0
    assert load_job(onyx_env.cfg, job.id).state == "cancelled"


def test_start_auto_requires_quote(onyx_env, monkeypatch, capsys) -> None:
    monkeypatch.setattr("onyx_pipeline.cli.spawn_runner", lambda _cfg: None)
    repo = onyx_env.roots / "auto-app"
    repo.mkdir()
    code = run(["start", "--kind", "code", "--repo", str(repo), "--auto", "skip gate"])
    assert code == 2
    assert "auto-quote" in capsys.readouterr().err.lower()


def test_start_auto_with_quote(onyx_env, monkeypatch, capsys) -> None:
    monkeypatch.setattr("onyx_pipeline.cli.spawn_runner", lambda _cfg: None)
    repo = onyx_env.roots / "auto-app2"
    repo.mkdir()
    code = run(
        [
            "start",
            "--kind",
            "code",
            "--repo",
            str(repo),
            "--auto",
            "--auto-quote",
            "David said ship without plan review",
            "do the thing",
        ]
    )
    assert code == 0
    job_id = capsys.readouterr().out.strip()
    job = load_job(onyx_env.cfg, job_id)
    assert job.flags.get("auto_quote") == "David said ship without plan review"


def test_idless_approve_one_waiting(onyx_env, monkeypatch) -> None:
    monkeypatch.setattr("onyx_pipeline.cli.spawn_runner", lambda _cfg: None)
    job = create_job(onyx_env.cfg, kind="code", task="t", repo=str(onyx_env.roots / "r"))
    (onyx_env.roots / "r").mkdir(exist_ok=True)
    job.state = "needs-approval"
    save_job(onyx_env.cfg, job)
    assert run(["approve"]) == 0
    assert load_job(onyx_env.cfg, job.id).flags["approved"] is True


def test_idless_approve_none(onyx_env, monkeypatch, capsys) -> None:
    monkeypatch.setattr("onyx_pipeline.cli.spawn_runner", lambda _cfg: None)
    code = run(["approve"])
    assert code == 1
    assert "no job is waiting" in capsys.readouterr().err


def test_idless_approve_several(onyx_env, monkeypatch, capsys) -> None:
    monkeypatch.setattr("onyx_pipeline.cli.spawn_runner", lambda _cfg: None)
    for n in range(2):
        job = create_job(onyx_env.cfg, kind="research", task=f"t{n}", repo=None)
        job.state = "needs-approval"
        save_job(onyx_env.cfg, job)
    code = run(["approve"])
    assert code == 1
    err = capsys.readouterr().err
    assert "several jobs" in err


def test_resolve_waiting_job_answer(onyx_env) -> None:
    job = create_job(onyx_env.cfg, kind="research", task="q", repo=None)
    job.state = "needs-answer"
    save_job(onyx_env.cfg, job)
    assert resolve_waiting_job(onyx_env.cfg, "needs-answer").id == job.id


def test_idless_revise_and_answer(onyx_env, monkeypatch) -> None:
    monkeypatch.setattr("onyx_pipeline.cli.spawn_runner", lambda _cfg: None)
    job = create_job(onyx_env.cfg, kind="code", task="t", repo=str(onyx_env.roots / "r2"))
    (onyx_env.roots / "r2").mkdir(exist_ok=True)
    job.state = "needs-approval"
    save_job(onyx_env.cfg, job)
    assert run(["revise", "smaller please"]) == 0
    assert load_job(onyx_env.cfg, job.id).state == "planning"

    job2 = create_job(onyx_env.cfg, kind="research", task="q2", repo=None)
    job2.state = "needs-answer"
    save_job(onyx_env.cfg, job2)
    assert run(["answer", "use JSON"]) == 0
    assert load_job(onyx_env.cfg, job2.id).state == "planning"


def test_duplicate_guard_matches_equivalent_repo_paths(onyx_env, monkeypatch, capsys) -> None:
    monkeypatch.setattr("onyx_pipeline.cli.spawn_runner", lambda _cfg: None)
    monkeypatch.setenv("ONYX_DUP_WINDOW_MIN", "30")
    cfg = load_config()
    repo = onyx_env.roots / "dup-app2"
    repo.mkdir()
    create_job(cfg, kind="code", task="Add logging to the widget service", repo=str(repo))
    odd_path = str(repo / ".." / "dup-app2")
    code = run(["start", "--kind", "code", "--repo", odd_path, "Add logging to the widget service"])
    assert code == 2
    assert "similar job" in capsys.readouterr().err
