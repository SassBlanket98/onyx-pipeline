"""Pipeline tests against stub worker binaries."""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

from onyx_pipeline.jobs import create_job, job_lock, load_job, lock_held
from onyx_pipeline.parse import parse_plan_trailers, parse_verdict
from onyx_pipeline.stages import answer_job, process_job, run_loop
from tests.helpers import make_git_repo


def _log(jobs: Path) -> str:
    path = jobs / ".worker-log"
    return path.read_text() if path.exists() else ""


def _summary_lines(jobs: Path, job_id: str) -> list[str]:
    text = (jobs / job_id / "summary.md").read_text()
    return [ln for ln in text.splitlines() if ln]


def test_small_code_job_end_to_end(onyx_env, monkeypatch):
    monkeypatch.setenv("STUB_CLAUDE_MODE", "ok")
    monkeypatch.setenv("STUB_REVIEW", "pass")
    repo = make_git_repo(onyx_env.roots)
    job = create_job(onyx_env.cfg, kind="code", repo=str(repo), task="Add a built marker file")
    run_loop(onyx_env.cfg)
    job = load_job(onyx_env.cfg, job.id)
    assert job.state == "done"
    assert job.branch == f"onyx/{job.id}"
    log = subprocess.run(
        ["git", "log", "--oneline", f"{job.base_branch}..{job.branch}"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    assert f"onyx-job {job.id}: building" in log.stdout
    verify = (onyx_env.jobs / job.id / "verify.log").read_text()
    assert "exit: 0" in verify
    lines = _summary_lines(onyx_env.jobs, job.id)
    assert 1 <= len(lines) <= 7
    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    assert branch.stdout.strip() == job.base_branch  # user's checkout never left
    assert job.worktree
    assert not Path(job.worktree).exists()


def test_claude_capped_falls_back_to_grok(onyx_env, monkeypatch):
    monkeypatch.setenv("STUB_CLAUDE_MODE", "capped")
    monkeypatch.setenv("STUB_PLAN_MODE", "ok")
    repo = make_git_repo(onyx_env.roots)
    job = create_job(onyx_env.cfg, kind="code", repo=str(repo), task="Fallback plan for the work")
    run_loop(onyx_env.cfg)
    job = load_job(onyx_env.cfg, job.id)
    assert job.state == "done"
    assert job.planner == "grok-fallback"
    summary = (onyx_env.jobs / job.id / "summary.md").read_text()
    assert "Planned by Grok (Claude unavailable)" in summary
    assert "claude" in _log(onyx_env.jobs)
    assert "cursor-agent" in _log(onyx_env.jobs)


def test_size_big_needs_approval_then_approve(onyx_env, monkeypatch):
    monkeypatch.setenv("STUB_CLAUDE_MODE", "big")
    repo = make_git_repo(onyx_env.roots)
    job = create_job(onyx_env.cfg, kind="code", repo=str(repo), task="A large change across files")
    run_loop(onyx_env.cfg)
    job = load_job(onyx_env.cfg, job.id)
    assert job.state == "done"


def test_open_questions_needs_answer_then_answer(onyx_env, monkeypatch):
    monkeypatch.setenv("STUB_CLAUDE_MODE", "questions-then-ok")
    repo = make_git_repo(onyx_env.roots)
    job = create_job(onyx_env.cfg, kind="code", repo=str(repo), task="Clarify the public API shape")
    run_loop(onyx_env.cfg)
    job = load_job(onyx_env.cfg, job.id)
    assert job.state == "needs-answer"
    answer_job(onyx_env.cfg, job.id, "Use JSON over HTTP.")
    task = (onyx_env.jobs / job.id / "task.md").read_text()
    assert "## Answers" in task
    assert "Use JSON over HTTP." in task
    run_loop(onyx_env.cfg)
    job = load_job(onyx_env.cfg, job.id)
    assert job.state == "done"


def test_fix_fail_then_pass_reaches_done(onyx_env, monkeypatch):
    monkeypatch.setenv("STUB_REVIEW", "fail-then-pass")
    repo = make_git_repo(onyx_env.roots)
    job = create_job(onyx_env.cfg, kind="code", repo=str(repo), task="Fix then pass the review")
    run_loop(onyx_env.cfg)
    job = load_job(onyx_env.cfg, job.id)
    assert job.state == "done"
    assert job.fix_rounds_used == 1
    log = subprocess.run(
        ["git", "log", "--oneline", f"{job.base_branch}..{job.branch}"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "fixing" in log.stdout


def test_fix_fail_twice_ends_failed_on_composer(onyx_env, monkeypatch):
    monkeypatch.setenv("STUB_REVIEW", "fail")
    repo = make_git_repo(onyx_env.roots)
    job = create_job(onyx_env.cfg, kind="code", repo=str(repo), task="Fail review twice then stop")
    run_loop(onyx_env.cfg)
    job = load_job(onyx_env.cfg, job.id)
    assert job.state == "failed"
    assert job.fix_rounds_used == 2
    worker = _log(onyx_env.jobs)
    force_lines = [ln for ln in worker.splitlines() if "--force" in ln]
    assert force_lines
    assert all("--model composer-2.5-fast " in ln for ln in force_lines)


def test_repo_outside_allowed_roots_fails(onyx_env):
    outside = onyx_env.tmp / "outside"
    outside.mkdir()
    (outside / "README").write_text("x\n")
    subprocess.run(["git", "init"], cwd=outside, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A"],
        cwd=outside,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "init"],
        cwd=outside,
        check=True,
        capture_output=True,
    )
    job = create_job(
        onyx_env.cfg, kind="code", repo=str(outside), task="Touch a repo that is forbidden"
    )
    run_loop(onyx_env.cfg)
    job = load_job(onyx_env.cfg, job.id)
    assert job.state == "failed"
    assert job.error and "allowed roots" in job.error


def test_user_checkout_untouched_with_uncommitted_file(onyx_env):
    repo = make_git_repo(onyx_env.roots, name="dirty")
    dirty = repo / "uncommitted.txt"
    dirty.write_text("leave me\n")
    before = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    job = create_job(onyx_env.cfg, kind="code", repo=str(repo), task="Must not touch the checkout")
    run_loop(onyx_env.cfg)
    job = load_job(onyx_env.cfg, job.id)
    assert job.state == "done"
    assert dirty.read_text() == "leave me\n"
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "uncommitted.txt" in status.stdout
    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    assert branch.stdout.strip() == before
    assert not branch.stdout.strip().startswith("onyx/")
    assert dirty.exists()
    assert not (repo / "onyx-built.txt").exists()


def test_non_git_dir_is_initialised(onyx_env):
    repo = onyx_env.roots / "plain"
    repo.mkdir()
    (repo / "README").write_text("hi\n")
    job = create_job(onyx_env.cfg, kind="code", repo=str(repo), task="Init a folder without git")
    run_loop(onyx_env.cfg)
    job = load_job(onyx_env.cfg, job.id)
    assert job.state == "done"
    assert (repo / ".git").exists()
    log = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "Initial commit (onyx-job)" in log.stdout
    assert job.branch == f"onyx/{job.id}"


def test_queue_and_lock_run_in_order(onyx_env):
    repo1 = make_git_repo(onyx_env.roots, name="one")
    repo2 = make_git_repo(onyx_env.roots, name="two")
    job1 = create_job(onyx_env.cfg, kind="code", repo=str(repo1), task="First queued job in order")
    job2 = create_job(onyx_env.cfg, kind="code", repo=str(repo2), task="Second queued job in order")
    order: list[str] = []
    overlapping = {"max": 0, "now": 0}
    original = process_job

    def wrapped(cfg, job):
        overlapping["now"] += 1
        overlapping["max"] = max(overlapping["max"], overlapping["now"])
        order.append(job.id)
        try:
            original(cfg, job)
        finally:
            overlapping["now"] -= 1

    import onyx_pipeline.stages as stages

    stages.process_job = wrapped  # type: ignore[method-assign]
    try:
        barrier_errors: list[BaseException] = []

        def runner() -> None:
            try:
                run_loop(onyx_env.cfg)
            except BaseException as exc:  # noqa: BLE001
                barrier_errors.append(exc)

        t1 = threading.Thread(target=runner)
        t2 = threading.Thread(target=runner)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        assert not barrier_errors
    finally:
        stages.process_job = original  # type: ignore[method-assign]

    assert order == [job1.id, job2.id]
    assert overlapping["max"] == 1
    assert load_job(onyx_env.cfg, job1.id).state == "done"
    assert load_job(onyx_env.cfg, job2.id).state == "done"
    assert not lock_held(onyx_env.cfg)


def test_research_kind_writes_report_and_disputed_on_fail(onyx_env, monkeypatch):
    monkeypatch.setenv("STUB_REVIEW", "fail")
    job = create_job(
        onyx_env.cfg,
        kind="research",
        repo=None,
        task="Check a public claim with sources",
    )
    run_loop(onyx_env.cfg)
    job = load_job(onyx_env.cfg, job.id)
    assert job.state == "done"
    assert job.report_path
    report = Path(job.report_path)
    assert report.exists()
    text = report.read_text()
    assert "## Disputed" in text
    assert "VERDICT: FAIL" in text


def test_missing_verdict_counts_as_fail():
    assert parse_verdict("all good\nno trailer") == "FAIL"
    assert parse_verdict("VERDICT: PASS\n") == "PASS"


def test_garbled_size_counts_as_big():
    size, _judgment, _verify = parse_plan_trailers("SIZE: enormous\nJUDGMENT: none\nVERIFY: true\n")
    assert size == "big"


def test_garbled_size_stops_at_needs_approval(onyx_env, monkeypatch):
    monkeypatch.setenv("STUB_CLAUDE_MODE", "garbled")
    repo = make_git_repo(onyx_env.roots)
    job = create_job(
        onyx_env.cfg, kind="code", repo=str(repo), task="Garbled size should count big"
    )
    run_loop(onyx_env.cfg)
    job = load_job(onyx_env.cfg, job.id)
    assert job.state == "done"


def test_missing_verdict_job_treats_review_as_fail(onyx_env, monkeypatch):
    monkeypatch.setenv("STUB_REVIEW", "missing")
    repo = make_git_repo(onyx_env.roots)
    job = create_job(
        onyx_env.cfg, kind="code", repo=str(repo), task="Review without a verdict line"
    )
    run_loop(onyx_env.cfg)
    job = load_job(onyx_env.cfg, job.id)
    assert job.verdict == "FAIL"
    assert job.state == "failed"


def test_lock_held_while_running(onyx_env):
    with job_lock(onyx_env.cfg.jobs_dir, blocking=False):
        assert lock_held(onyx_env.cfg)


def test_verify_shell_state_carries_between_chained_commands(onyx_env, tmp_path) -> None:
    from onyx_pipeline.stages import _run_in_one_shell

    results = _run_in_one_shell(
        ["d=$(mktemp -d)", 'test -d "$d"', "echo hi && false", "echo after"], tmp_path, 30
    )
    assert [code for _cmd, code, _tail in results] == [0, 0, 1, 0]
    assert "hi" in results[2][2]


def _ask_lines(jobs: Path) -> list[str]:
    return [ln for ln in _log(jobs).splitlines() if "--mode ask" in ln and "--model" in ln]


def _investigator(onyx_env, monkeypatch, task: str, deep: bool | None) -> str:
    monkeypatch.setenv("STUB_CLAUDE_MODE", "ok")
    monkeypatch.setenv("STUB_REVIEW", "pass")
    log = onyx_env.jobs / ".worker-log"
    if log.exists():
        log.unlink()
    repo = make_git_repo(onyx_env.roots)
    job = create_job(onyx_env.cfg, kind="code", repo=str(repo), task=task, deep=deep)
    run_loop(onyx_env.cfg)
    lines = _ask_lines(onyx_env.jobs)
    assert lines
    saved = load_job(onyx_env.cfg, job.id)
    assert isinstance(saved.flags["triage"], dict)  # the decision is kept on the job
    return lines[0]  # the investigate call comes first


def test_plain_task_investigates_with_composer(onyx_env, monkeypatch):
    line = _investigator(onyx_env, monkeypatch, "Add a built marker file", None)
    assert "--model composer-2.5-fast " in line


def test_risky_task_is_sent_to_grok_automatically(onyx_env, monkeypatch):
    line = _investigator(onyx_env, monkeypatch, "Add a login with passwords for two users", None)
    assert "--model cursor-grok-4.6-high " in line


def test_deep_forces_grok(onyx_env, monkeypatch):
    line = _investigator(onyx_env, monkeypatch, "Add a built marker file", True)
    assert "--model cursor-grok-4.6-high " in line


def test_quick_forces_composer(onyx_env, monkeypatch):
    task = "Add a login with passwords for two users"
    line = _investigator(onyx_env, monkeypatch, task, False)
    assert "--model composer-2.5-fast " in line
