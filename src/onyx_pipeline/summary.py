"""Template summary.md with no model involved."""

from __future__ import annotations

from pathlib import Path

from onyx_pipeline.config import Config
from onyx_pipeline.jobs import Job, job_dir


def _commit_count(job: Job) -> int:
    if not job.repo or not job.base_branch or not job.branch:
        return 0
    import subprocess

    result = subprocess.run(
        ["git", "rev-list", "--count", f"{job.base_branch}..{job.branch}"],
        cwd=job.repo,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return 0
    try:
        return int(result.stdout.strip() or "0")
    except ValueError:
        return 0


def _verify_text(job: Job) -> str:
    if not job.verify_results:
        return "n/a"
    first = job.verify_results[0]
    if first.get("skipped"):
        return "skipped (no VERIFY line)"
    parts: list[str] = []
    for item in job.verify_results:
        cmd = item.get("command")
        code = item.get("exit_code")
        suffix = " (could not run)" if item.get("could_not_run") else ""
        parts.append(f"{cmd}=>{code}{suffix}")
    return ", ".join(parts)


def _parts_line(job: Job) -> str | None:
    if job.split_note:
        # A planner that wrote a PARTS heading with no parts under it: the job was built as
        # one piece anyway, so once it is done the note tells David nothing. It stays in state.
        if job.split_note == "malformed PARTS section" and job.state == "done":
            return None
        return job.split_note
    if not job.parts:
        return None
    rebuilt = sum(1 for item in job.parts if item.get("state") == "rebuilt")
    return f"Built in {len(job.parts)} parts ({rebuilt} rebuilt after a merge clash)"


def _david_text(cfg: Config, job: Job) -> str:
    if job.state == "needs-approval":
        return f'approve (onyx-job approve {job.id}) or changes (onyx-job revise {job.id} "...")'
    if job.state == "needs-answer":
        from onyx_pipeline.parse import parse_open_questions

        plan_path = job_dir(cfg, job.id) / "plan.md"
        questions: list[str] = []
        if plan_path.exists():
            questions = parse_open_questions(plan_path.read_text())
        if questions:
            return "answer: " + "; ".join(questions)
        return f'answer (onyx-job answer {job.id} "...")'
    if job.state == "failed":
        return job.error or "inspect the failure"
    return "nothing"


def render_summary(cfg: Config, job: Job) -> str:
    if job.kind == "research":
        loc = job.report_path or "n/a"
        loc_line = f"Report: {loc}"
    else:
        n = _commit_count(job)
        loc_line = f"Branch: {job.branch or 'n/a'} ({n} commits)"
        if job.worktree and Path(job.worktree).exists():
            loc_line = f"Branch: {job.branch or 'n/a'} at {job.worktree} ({n} commits)"
    only = job.flags.get("only")
    result = f"Result: {job.state} ({only} only)" if only else f"Result: {job.state}"
    lines = [
        result,
        loc_line,
        f"Verify: {_verify_text(job)}",
        f"Verdict: {job.verdict or 'n/a'}",
    ]
    if job.planner == "grok-fallback":
        lines.append("Planned by Grok (Claude unavailable)")
    parts_line = _parts_line(job)
    if parts_line:
        lines.append(parts_line)
    lines.append(f"David: {_david_text(cfg, job)}")
    return "\n".join(lines[:7]) + "\n"


def write_summary(cfg: Config, job: Job) -> Path:
    path = job_dir(cfg, job.id) / "summary.md"
    path.write_text(render_summary(cfg, job))
    return path
