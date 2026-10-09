"""Re-run a single stage of a finished job."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from onyx_pipeline.cli import run
from onyx_pipeline.jobs import create_job, load_job, save_job
from onyx_pipeline.stages import rerun_job, run_loop
from tests.helpers import make_git_repo


def _log(jobs: Path) -> str:
    path = jobs / ".worker-log"
    return path.read_text() if path.exists() else ""


def _new_log(before: str, jobs: Path) -> str:
    return _log(jobs)[len(before) :]


def _kinds(text: str) -> list[str]:
    kinds: list[str] = []
    for line in text.splitlines():
        if line.startswith("claude "):
            kinds.append("plan")
        elif line.startswith("cursor-agent ") and "--force" in line:
            kinds.append("build")
        elif line.startswith("cursor-agent "):
            kinds.append("investigate")
        elif line.startswith("codex ") and "--skip-git-repo-check" in line:
            kinds.append("fact-check")
        elif line.startswith("codex "):
            kinds.append("review")
    return kinds


def _finish_code(onyx_env, monkeypatch, *, name: str, task: str):
    monkeypatch.setenv("STUB_CLAUDE_MODE", "ok")
    monkeypatch.setenv("STUB_REVIEW", "pass")
    repo = make_git_repo(onyx_env.roots, name)
    job = create_job(onyx_env.cfg, kind="code", repo=str(repo), task=task)
    run_loop(onyx_env.cfg)
    job = load_job(onyx_env.cfg, job.id)
    assert job.state == "done"
    return repo, job


def test_review_only_on_done_job(onyx_env, monkeypatch):
    repo, job = _finish_code(
        onyx_env, monkeypatch, name="review-ok", task="Add a built marker for review-only"
    )
    before = _log(onyx_env.jobs)
    rerun_job(onyx_env.cfg, job.id, "review")
    run_loop(onyx_env.cfg)
    job = load_job(onyx_env.cfg, job.id)
    assert job.state == "done"
    assert job.verdict == "PASS"
    kinds = _kinds(_new_log(before, onyx_env.jobs))
    assert kinds
    assert set(kinds) == {"review"}
    assert "investigate" not in kinds
    assert "plan" not in kinds
    assert "build" not in kinds
    assert job.worktree
    assert not Path(job.worktree).exists()
    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    assert branch.stdout.strip() == job.base_branch


def test_review_only_that_fails(onyx_env, monkeypatch):
    _repo, job = _finish_code(
        onyx_env, monkeypatch, name="review-fail", task="Review-only failure path"
    )
    used = job.fix_rounds_used
    before = _log(onyx_env.jobs)
    monkeypatch.setenv("STUB_REVIEW", "fail")
    rerun_job(onyx_env.cfg, job.id, "review")
    run_loop(onyx_env.cfg)
    job = load_job(onyx_env.cfg, job.id)
    assert job.state == "failed"
    assert job.error and "review-only" in job.error
    assert job.fix_rounds_used == used
    kinds = _kinds(_new_log(before, onyx_env.jobs))
    assert "build" not in kinds
    assert "review" in kinds


def test_rerun_review_with_fix(onyx_env, monkeypatch):
    _repo, job = _finish_code(
        onyx_env, monkeypatch, name="review-fix", task="Review then fix rounds"
    )
    before = _log(onyx_env.jobs)
    monkeypatch.setenv("STUB_REVIEW", "fail")
    rerun_job(onyx_env.cfg, job.id, "review", fix=True)
    run_loop(onyx_env.cfg)
    job = load_job(onyx_env.cfg, job.id)
    kinds = _kinds(_new_log(before, onyx_env.jobs))
    assert "build" in kinds
    assert job.fix_rounds_used >= 1


def test_verify_only(onyx_env, monkeypatch):
    _repo, job = _finish_code(
        onyx_env, monkeypatch, name="verify-only", task="Re-run verify commands"
    )
    before = _log(onyx_env.jobs)
    rerun_job(onyx_env.cfg, job.id, "verify")
    run_loop(onyx_env.cfg)
    job = load_job(onyx_env.cfg, job.id)
    kinds = _kinds(_new_log(before, onyx_env.jobs))
    assert "review" not in kinds
    summary = (onyx_env.jobs / job.id / "summary.md").read_text()
    first = summary.splitlines()[0]
    assert first.endswith("(verify only)")


def test_research_fact_check_rerun(onyx_env, monkeypatch):
    monkeypatch.setenv("STUB_CLAUDE_MODE", "ok")
    monkeypatch.setenv("STUB_REVIEW", "pass")
    job = create_job(
        onyx_env.cfg,
        kind="research",
        repo=None,
        task="Check a public claim again",
    )
    run_loop(onyx_env.cfg)
    job = load_job(onyx_env.cfg, job.id)
    assert job.state == "done"
    before = _log(onyx_env.jobs)
    rerun_job(onyx_env.cfg, job.id, "fact-check")
    run_loop(onyx_env.cfg)
    job = load_job(onyx_env.cfg, job.id)
    assert job.state == "done"
    kinds = _kinds(_new_log(before, onyx_env.jobs))
    assert kinds
    assert set(kinds) == {"fact-check"}


def test_rerun_guards(onyx_env):
    queued = create_job(onyx_env.cfg, kind="code", repo=None, task="Still sitting in the queue")
    with pytest.raises(ValueError, match="only a finished job"):
        rerun_job(onyx_env.cfg, queued.id, "review")

    research = create_job(onyx_env.cfg, kind="research", repo=None, task="Research kind guard")
    research.state = "done"
    save_job(onyx_env.cfg, research)
    with pytest.raises(ValueError, match="does not apply"):
        rerun_job(onyx_env.cfg, research.id, "review")

    code = create_job(onyx_env.cfg, kind="code", repo=None, task="Code kind fix guard")
    code.state = "done"
    save_job(onyx_env.cfg, code)
    with pytest.raises(ValueError, match="--fix only applies"):
        rerun_job(onyx_env.cfg, code.id, "verify", fix=True)


def test_cli_unknown_job_returns_1(onyx_env):
    assert run(["rerun", "nope", "--stage", "review"]) == 1


def test_git_prepare_keeps_recorded_base(onyx_env, monkeypatch):
    repo, job = _finish_code(
        onyx_env, monkeypatch, name="keep-base", task="Keep the original base branch"
    )
    recorded = job.base_branch
    subprocess.run(
        ["git", "checkout", "-b", "other-work"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    rerun_job(onyx_env.cfg, job.id, "review")
    run_loop(onyx_env.cfg)
    job = load_job(onyx_env.cfg, job.id)
    assert job.base_branch == recorded


def test_rerun_rejects_job_whose_build_never_finished(onyx_env, monkeypatch):
    monkeypatch.setenv("STUB_CLAUDE_MODE", "ok")
    monkeypatch.setenv("STUB_REVIEW", "pass")
    repo = make_git_repo(onyx_env.roots)
    job = create_job(onyx_env.cfg, kind="code", repo=str(repo), task="Add a built marker file")
    run_loop(onyx_env.cfg)
    job = load_job(onyx_env.cfg, job.id)
    job.state = "failed"
    job.stages = [s for s in job.stages if s["state"] not in {"verifying", "reviewing", "done"}]
    save_job(onyx_env.cfg, job)
    with pytest.raises(ValueError, match="never built"):
        rerun_job(onyx_env.cfg, job.id, "review")


def test_fact_check_rerun_replaces_old_dispute_and_uses_local_copy(onyx_env, monkeypatch):
    monkeypatch.setenv("STUB_CLAUDE_MODE", "ok")
    monkeypatch.setenv("STUB_REVIEW", "fail")
    job = create_job(onyx_env.cfg, kind="research", repo=None, task="Look into widgets")
    run_loop(onyx_env.cfg)
    job = load_job(onyx_env.cfg, job.id)
    report = Path(job.report_path)
    assert "## Disputed" in report.read_text()

    monkeypatch.setenv("STUB_REVIEW", "pass")
    rerun_job(onyx_env.cfg, job.id, "fact-check")
    run_loop(onyx_env.cfg)
    job = load_job(onyx_env.cfg, job.id)
    assert job.state == "done"
    assert job.verdict == "PASS"
    assert "## Disputed" not in report.read_text()

    report.unlink()
    rerun_job(onyx_env.cfg, job.id, "fact-check")
    run_loop(onyx_env.cfg)
    job = load_job(onyx_env.cfg, job.id)
    assert job.state == "done"
    assert job.verdict == "PASS"


def test_fact_check_corrections_verdict(onyx_env, monkeypatch):
    monkeypatch.setenv("STUB_CLAUDE_MODE", "ok")
    monkeypatch.setenv("STUB_REVIEW", "corrections")
    job = create_job(onyx_env.cfg, kind="research", repo=None, task="Look into widgets")
    run_loop(onyx_env.cfg)
    job = load_job(onyx_env.cfg, job.id)
    assert job.state == "done"
    assert job.verdict == "CORRECTIONS"
    text = Path(job.report_path).read_text()
    assert "## Corrections from the fact-check" in text
    assert "Step 4 names the wrong tool." in text
    assert "VERDICT" not in text
    assert "## Disputed" not in text

    monkeypatch.setenv("STUB_REVIEW", "pass")
    rerun_job(onyx_env.cfg, job.id, "fact-check")
    run_loop(onyx_env.cfg)
    assert "## Corrections" not in Path(job.report_path).read_text()


def test_corrections_is_not_a_code_review_verdict(onyx_env, monkeypatch):
    from onyx_pipeline.parse import parse_verdict

    assert parse_verdict("notes\nVERDICT: CORRECTIONS\n") == "FAIL"
    assert parse_verdict("notes\nVERDICT: CORRECTIONS\n", corrections=True) == "CORRECTIONS"
