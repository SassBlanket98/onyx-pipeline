"""State machine for code and research jobs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from onyx_pipeline import prompts
from onyx_pipeline.config import Config, load_config
from onyx_pipeline.deliver import compose_stop_message, deliver_stop
from onyx_pipeline.jobs import (
    SAVE_LOCK,
    Job,
    LockHeldError,
    job_dir,
    job_lock,
    list_jobs,
    load_job,
    lock_held,
    next_runnable,
    save_job,
    slugify_task,
    transition,
)
from onyx_pipeline.notify import in_quiet_hours, notify_job
from onyx_pipeline.ops_brief import parse_brief, render_operator_brief
from onyx_pipeline.parse import (
    PlanPart,
    parse_open_questions,
    parse_plan_parts,
    parse_plan_trailers,
    parse_verdict,
    plan_text_before_parts,
    planned_files,
)
from onyx_pipeline.planview import render_plan_view
from onyx_pipeline.progress import start_checklist, tick_checklist
from onyx_pipeline.summary import write_summary
from onyx_pipeline.telegram import send_message
from onyx_pipeline.triage import triage_task
from onyx_pipeline.workers import (
    COMPOSER_MODEL,
    GROK_MODEL,
    REPORT_MODEL,
    STALLED,
    Watch,
    brief_ops,
    build,
    build_model_for,
    changed_paths,
    fact_check,
    investigate,
    kill_active_workers,
    operate_ops,
    plan_claude,
    plan_grok,
    review,
    run_capture,
    verify_ops,
)

CAPPED_RE = re.compile(r"usage limit|rate limit|quota|limit reached", re.I)
GIT_NAME = "onyx-job"
GIT_EMAIL = "onyx-job@local"


# The section the fact-check appends to a report; a rerun cuts it off again.
_FACT_CHECK_SECTION_RE = re.compile(r"\n## (Disputed|Corrections from the fact-check)\n")


class JobFailed(Exception):
    """A job cannot continue."""


def claude_capped_or_failed(exit_code: int, output: str) -> bool:
    if exit_code != 0:
        return True
    if not output.strip():
        return True
    return CAPPED_RE.search(output) is not None


def is_under_allowed_roots(repo: Path, roots: tuple[Path, ...]) -> bool:
    resolved = repo.resolve()
    for root in roots:
        try:
            resolved.relative_to(root.expanduser().resolve())
            return True
        except ValueError:
            continue
    return False


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", f"user.name={GIT_NAME}", "-c", f"user.email={GIT_EMAIL}", *args],
        cwd=repo,
        check=check,
        capture_output=True,
        text=True,
    )


JUNK_PATHSPECS = (
    ":(exclude,glob)**/__pycache__/**",
    ":(exclude,glob)**/*.pyc",
    ":(exclude,glob)**/.pytest_cache/**",
    ":(exclude,glob)**/.ruff_cache/**",
    ":(exclude,glob)**/.venv/**",
    ":(exclude,glob)**/node_modules/**",
)


def commit_if_changes(repo: Path, message: str) -> None:
    _git(repo, "add", "-A", "--", ".", *JUNK_PATHSPECS)
    staged = _git(repo, "diff", "--cached", "--quiet", check=False)
    if staged.returncode == 0:
        return
    _git(repo, "commit", "-m", message)


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _log_watchdog_stall(
    directory: Path, stage: str, model: str, idle_limit: int, action: str
) -> None:
    ts = datetime.now().astimezone().isoformat(timespec="seconds")
    with (directory / "runner.log").open("a", encoding="utf-8") as fh:
        fh.write(f"{ts} watchdog: {stage} on {model} stalled after {idle_limit}s; {action}\n")


def _record_stall(job: Job, stage: str, model: str, action: str) -> None:
    with SAVE_LOCK:
        job.flags.setdefault("stalls", []).append(
            {"stage": stage, "model": model, "action": action}
        )


def _build_watched(
    cfg: Config,
    job: Job,
    directory: Path,
    repo: Path,
    model: str,
    brief: str,
    *,
    stage: str = "building",
    log_name: str | None = None,
) -> int:
    base = _head(repo)
    watch = Watch(
        repo=repo,
        base=base,
        idle_limit=cfg.idle_limit,
        poll=cfg.watch_poll,
        on_tick=lambda paths: tick_checklist(cfg, job, paths),
    )
    code, _out = build(
        cfg,
        directory,
        repo,
        model,
        brief,
        stage=stage,
        log_name=log_name,
        watch=watch,
    )
    if code != STALLED:
        return code
    if changed_paths(repo, base):
        _record_stall(job, stage, model, "continued")
        save_job(cfg, job)
        _log_watchdog_stall(directory, stage, model, cfg.idle_limit, "continued")
        return 0
    fail_twice = f"{stage} stalled: no file changes or output for {cfg.idle_limit}s, twice"
    fail_once = f"{stage} stalled: no file changes or output for {cfg.idle_limit}s, once"
    if model != COMPOSER_MODEL:
        _record_stall(job, stage, model, "retried on composer")
        job.build_model = COMPOSER_MODEL
        save_job(cfg, job)
        _log_watchdog_stall(directory, stage, model, cfg.idle_limit, "retried on composer")
        retry_watch = Watch(
            repo=repo,
            base=base,
            idle_limit=cfg.idle_limit,
            poll=cfg.watch_poll,
            on_tick=lambda paths: tick_checklist(cfg, job, paths),
        )
        code, _out = build(
            cfg,
            directory,
            repo,
            COMPOSER_MODEL,
            brief,
            stage=stage,
            log_name=log_name,
            watch=retry_watch,
        )
        if code != STALLED:
            return code
        if changed_paths(repo, base):
            _record_stall(job, stage, COMPOSER_MODEL, "continued")
            save_job(cfg, job)
            _log_watchdog_stall(directory, stage, COMPOSER_MODEL, cfg.idle_limit, "continued")
            return 0
        raise JobFailed(fail_twice)
    raise JobFailed(fail_once)


def guard_branch(repo: Path, job: Job, before: str, expected_branch: str | None = None) -> None:
    """Fail the job if a worker switched branch or rewrote the history it started from."""
    current = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    expected = expected_branch or job.branch
    if expected and current != expected:
        raise JobFailed(f"worker switched branch to {current!r}; expected {expected!r}")
    ancestor = _git(repo, "merge-base", "--is-ancestor", before, "HEAD", check=False)
    if ancestor.returncode != 0:
        raise JobFailed("worker rewrote git history (reset, amend or rebase)")


def git_prepare(cfg: Config, job: Job) -> None:
    if not job.repo:
        raise JobFailed("repo is required for code jobs")
    repo = Path(job.repo).expanduser()
    if not repo.exists():
        raise JobFailed(f"repo does not exist: {repo}")
    resolved = repo.resolve()
    if not is_under_allowed_roots(resolved, cfg.allowed_roots):
        raise JobFailed(f"repo is outside allowed roots: {resolved}")
    git_meta = resolved / ".git"
    if not git_meta.exists():
        _git(resolved, "init")
        _git(resolved, "add", "-A")
        _git(resolved, "commit", "--allow-empty", "-m", "Initial commit (onyx-job)")
    base = job.base_branch or _git(resolved, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    branch = f"onyx/{job.id}"
    worktree = job_dir(cfg, job.id) / "worktree"
    existing = _git(resolved, "rev-parse", "--verify", branch, check=False)
    if not worktree.exists():
        if existing.returncode == 0:
            added = _git(resolved, "worktree", "add", str(worktree), branch, check=False)
        else:
            added = _git(
                resolved, "worktree", "add", str(worktree), "-b", branch, base, check=False
            )
        if added.returncode != 0:
            detail = (added.stderr or added.stdout).strip()
            raise JobFailed(f"git worktree add failed: {detail}")
    job.base_branch = base
    job.branch = branch
    job.repo = str(resolved)
    job.worktree = str(worktree.resolve())
    save_job(cfg, job)


def _read(path: Path) -> str:
    return path.read_text() if path.exists() else ""


def _cleanup_part_worktrees(cfg: Config, job: Job) -> None:
    if not job.repo or not job.branch:
        return
    repo = Path(job.repo)
    parts_root = job_dir(cfg, job.id) / "parts"
    if parts_root.exists():
        for child in sorted(parts_root.iterdir()):
            _git(repo, "worktree", "remove", "--force", str(child), check=False)
    listed = _git(repo, "branch", "--list", f"{job.branch}-p*", check=False)
    for line in listed.stdout.splitlines():
        name = line.strip().lstrip("* ").split()[0]
        if name.startswith(f"{job.branch}-p"):
            _git(repo, "branch", "-D", name, check=False)


def _remove_job_worktree(job: Job) -> None:
    if not job.repo or not job.worktree:
        return
    repo = Path(job.repo)
    _git(repo, "worktree", "remove", "--force", job.worktree, check=False)
    _git(repo, "worktree", "prune", check=False)


def _stop(cfg: Config, job: Job, state: str, error: str | None = None) -> None:
    if error:
        job.error = error
    if state in {"done", "cancelled"}:
        _cleanup_part_worktrees(cfg, job)
        _remove_job_worktree(job)
    elif state == "failed":
        _cleanup_part_worktrees(cfg, job)
    transition(cfg, job, state)
    write_summary(cfg, job)
    delivered = deliver_stop(cfg, job)
    if delivered:
        job.flags["delivered"] = job.state
    job.flags["settled"] = job.state
    save_job(cfg, job)
    if not delivered:
        notify_job(cfg, job)


_TASK_WORD_RE = re.compile(r"[a-z0-9]{3,}", re.I)


def _task_word_set(task: str) -> set[str]:
    return {m.group(0).lower() for m in _TASK_WORD_RE.finditer(task)}


def task_jaccard_similarity(a: str, b: str) -> float:
    sa, sb = _task_word_set(a), _task_word_set(b)
    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _norm_repo(repo: str | None) -> str | None:
    return str(Path(repo).expanduser().resolve()) if repo else None


def find_duplicate_job(
    cfg: Config,
    *,
    kind: str,
    repo: str | None,
    task: str,
    window_min: int,
) -> Job | None:
    if window_min <= 0:
        return None
    cutoff = datetime.now().astimezone() - timedelta(minutes=window_min)
    repo_key = _norm_repo(repo) if kind == "code" else kind
    for job in list_jobs(cfg):
        if job.kind != kind:
            continue
        job_key = _norm_repo(job.repo) if job.kind == "code" else job.kind
        if job_key != repo_key:
            continue
        if not job.stages:
            continue
        try:
            created = datetime.fromisoformat(str(job.stages[0]["at"]))
        except ValueError:
            continue
        if created.tzinfo is None:
            created = created.astimezone()
        if created < cutoff:
            continue
        if task_jaccard_similarity(task, job.task) >= 0.7:
            return job
    return None


def _waiting_label(state: str) -> str:
    if state == "needs-approval":
        return "approval"
    if state == "needs-answer":
        return "an answer"
    return state


def resolve_waiting_job(cfg: Config, state: str) -> Job:
    matches = [j for j in list_jobs(cfg) if j.state == state]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError(f"no job is waiting for {_waiting_label(state)}")
    ids = ", ".join(j.id for j in matches)
    raise ValueError(f"several jobs are waiting: {ids}; pass a job id")


def _run_plan(cfg: Config, job: Job) -> str:
    directory = job_dir(cfg, job.id)
    workspace = Path(job.repo).expanduser() if job.repo else directory
    prompt = prompts.plan_prompt(_read(directory / "task.md"), _read(directory / "evidence.md"))
    code, out = plan_claude(cfg, directory, workspace, prompt)
    if claude_capped_or_failed(code, out):
        code, out = plan_claude(cfg, directory, workspace, prompt)
    if claude_capped_or_failed(code, out):
        job.planner = "grok-fallback"
        code, out = plan_grok(cfg, directory, workspace, prompt)
        if code != 0 or not out.strip():
            raise JobFailed("planning failed (Claude capped; Grok fallback failed)")
    else:
        job.planner = "claude"
    directory.joinpath("plan.md").write_text(out)
    save_job(cfg, job)
    return out


def verify_commands(verify_line: str, job: Job) -> list[str]:
    """Split the VERIFY line and make it safe to run in the job's worktree.

    Planners see the user's repo path, so drop `cd` steps and point any absolute
    reference to the repo at the worktree instead.
    """
    commands = [part.strip() for part in verify_line.split("&&") if part.strip()]
    out: list[str] = []
    for cmd in commands:
        if cmd == "cd" or cmd.startswith("cd "):
            continue
        if job.repo and job.worktree:
            repo_abs = str(Path(job.repo).expanduser().resolve())
            cmd = cmd.replace(repo_abs, str(job.worktree)).replace(job.repo, str(job.worktree))
        out.append(cmd)
    return out


def _run_in_one_shell(commands: list[str], cwd: Path, timeout: int) -> list[tuple[str, int, str]]:
    """Run every command in one bash process so shell state (`d=$(mktemp -d)`) carries over.

    Each command still gets its own exit code and output tail, and a failure doesn't stop
    the later commands, as before.
    """
    script = "".join(
        f"printf '\\n@@ONYX_BEGIN {i}\\n'; {{ {cmd}\n}} 2>&1; printf '\\n@@ONYX_END {i} %d\\n' $?\n"
        for i, cmd in enumerate(commands)
    )
    code, out, err = run_capture(["bash", "-c", script], cwd=cwd, timeout=timeout)
    results: list[tuple[str, int, str]] = []
    for i, cmd in enumerate(commands):
        match = re.search(rf"@@ONYX_BEGIN {i}\n(.*?)\n@@ONYX_END {i} (\d+)\n", out, flags=re.DOTALL)
        if match:
            tail = "\n".join(match.group(1).splitlines()[-40:])
            results.append((cmd, int(match.group(2)), tail))
        else:
            tail = "\n".join((out + err).splitlines()[-40:])
            results.append((cmd, code or 1, tail))
    return results


_COULD_NOT_RUN_PHRASES = (
    "command not found",
    "no module named",
)


def could_not_run(exit_code: object, output: str) -> bool:
    """True when a verify command never got as far as running the tests.

    Exit code 127 (command not found) or 126 (not executable) means the shell
    never started the command. Otherwise the output must show an environment
    problem as the whole failure: at most 3 non-empty lines, one of which
    contains "command not found" or "No module named" (case-insensitive). Long
    output is a real test failure, even if it mentions one of those phrases. A
    missing file is not counted: a command that ran and could not find a file
    it needed has failed for real.
    """
    try:
        code = int(exit_code)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        code = 0
    if code in (126, 127):
        return True
    lines = [line for line in output.splitlines() if line.strip()]
    if not lines or len(lines) > 3:
        return False
    lowered = output.lower()
    return any(phrase in lowered for phrase in _COULD_NOT_RUN_PHRASES)


def _output_tail(output: str) -> str:
    """Last 20 lines of combined output, capped at 1500 characters."""
    return "\n".join(output.splitlines()[-20:])[-1500:]


def _run_verify(cfg: Config, job: Job, verify_line: str | None) -> bool:
    log_path = job_dir(cfg, job.id) / "verify.log"
    if not verify_line:
        log_path.write_text("skipped (no VERIFY line)\n")
        job.verify_results = [{"command": None, "exit_code": None, "skipped": True}]
        save_job(cfg, job)
        return True
    workdir = (
        Path(job.worktree)
        if job.worktree
        else (Path(job.repo) if job.repo else job_dir(cfg, job.id))
    )
    repo = workdir
    commands = verify_commands(verify_line, job)
    chunks: list[str] = []
    results: list[dict[str, object]] = []
    all_ok = True
    for cmd, code, tail in _run_in_one_shell(commands, repo, cfg.verify_timeout):
        chunks.append(f"$ {cmd}\nexit: {code}\n{tail}\n")
        entry: dict[str, object] = {
            "command": cmd,
            "exit_code": code,
            "could_not_run": code != 0 and could_not_run(code, tail),
        }
        if code != 0:
            entry["tail"] = _output_tail(tail)
        results.append(entry)
        if code != 0:
            all_ok = False
    log_path.write_text("\n".join(chunks) + ("\n" if chunks else ""))
    job.verify_results = results
    save_job(cfg, job)
    return all_ok


def _verify_ok(job: Job) -> bool:
    if not job.verify_results:
        return True
    if job.verify_results[0].get("skipped"):
        return True
    return all(int(item.get("exit_code") or 0) == 0 for item in job.verify_results)


def _part_state(job: Job, n: int) -> str | None:
    for item in job.parts:
        if int(item["n"]) == n:
            state = item.get("state")
            return str(state) if state is not None else None
    return None


def _set_part(job: Job, n: int, state: str, model: str | None = None) -> None:
    for item in job.parts:
        if int(item["n"]) == n:
            item["state"] = state
            if model is not None:
                item["model"] = model
            return


def _done_part_numbers(job: Job) -> set[int]:
    return {int(item["n"]) for item in job.parts if item.get("state") in {"merged", "rebuilt"}}


def _remove_part_branch(repo: Path, job: Job, n: int, part_wt: Path) -> None:
    _git(repo, "worktree", "remove", "--force", str(part_wt), check=False)
    _git(repo, "branch", "-D", f"{job.branch}-p{n}", check=False)


def _warn_extra_files(directory: Path, part: PlanPart, changed: list[str]) -> None:
    extra = [path for path in changed if path not in set(part.files)]
    if not extra:
        return
    log_path = directory / f"build-p{part.n}.log"
    previous = log_path.read_text() if log_path.exists() else ""
    log_path.write_text(
        previous + f"\n[onyx-job] warning: part {part.n} changed files outside its FILES list: "
        f"{', '.join(extra)}\n"
    )


def _part_brief(parts: list[PlanPart], part: PlanPart, plan_text: str, extra: str = "") -> str:
    others = [(p.title, p.files) for p in parts if p.n != part.n]
    return prompts.part_brief(
        n=part.n,
        title=part.title,
        steps=part.text,
        files=part.files,
        others=others,
        decisions=plan_text_before_parts(plan_text),
        extra=extra,
    )


def _run_one_part(
    cfg: Config,
    job: Job,
    directory: Path,
    job_wt: Path,
    parts: list[PlanPart],
    part: PlanPart,
    plan_text: str,
    merge_lock: threading.Lock,
) -> str:
    repo = Path(job.repo) if job.repo else directory
    part_wt = directory / "parts" / str(part.n)
    part_branch = f"{job.branch}-p{part.n}"
    _remove_part_branch(repo, job, part.n, part_wt)
    added = _git(
        repo,
        "worktree",
        "add",
        str(part_wt),
        "-b",
        part_branch,
        job.branch or "HEAD",
        check=False,
    )
    if added.returncode != 0:
        raise JobFailed((added.stderr or added.stdout).strip() or "worktree add failed")
    model = build_model_for(cfg, part.judgment)
    brief = _part_brief(parts, part, plan_text)
    before = _head(part_wt)
    code = _build_watched(
        cfg,
        job,
        directory,
        part_wt,
        model,
        brief,
        log_name=f"build-p{part.n}.log",
    )
    if code != 0:
        raise JobFailed(f"build failed with exit {code}")
    guard_branch(part_wt, job, before, expected_branch=part_branch)
    commit_if_changes(part_wt, f"onyx-job {job.id}: part {part.n} {part.title}")
    after = _head(part_wt)
    if after != before:
        changed = _git(part_wt, "diff", "--name-only", before, after).stdout.splitlines()
        _warn_extra_files(directory, part, changed)
    with merge_lock:
        merged = _git(
            job_wt,
            "merge",
            "--no-ff",
            part_branch,
            "-m",
            f"onyx-job {job.id}: merge part {part.n}",
            check=False,
        )
        if merged.returncode != 0:
            _git(job_wt, "merge", "--abort", check=False)
            return "clashed"
        _remove_part_branch(repo, job, part.n, part_wt)
        return "merged"


def _rebuild_part(
    cfg: Config,
    job: Job,
    directory: Path,
    job_wt: Path,
    parts: list[PlanPart],
    part: PlanPart,
    plan_text: str,
) -> None:
    model = build_model_for(cfg, part.judgment)
    job.build_model = model
    brief = _part_brief(
        parts,
        part,
        plan_text,
        extra="The other parts are already merged; integrate with them.",
    )
    before = _head(job_wt)
    code = _build_watched(
        cfg,
        job,
        directory,
        job_wt,
        model,
        brief,
        log_name=f"build-p{part.n}.log",
    )
    if code != 0:
        raise JobFailed(f"part {part.n} ({part.title}) rebuild failed with exit {code}")
    guard_branch(job_wt, job, before)
    commit_if_changes(job_wt, f"onyx-job {job.id}: part {part.n} {part.title}")
    repo = Path(job.repo) if job.repo else directory
    _remove_part_branch(repo, job, part.n, directory / "parts" / str(part.n))


def _build_parts(
    cfg: Config,
    job: Job,
    directory: Path,
    job_wt: Path,
    parts: list[PlanPart],
    plan_text: str,
) -> None:
    merge_lock = threading.Lock()
    state_lock = threading.Lock()
    fail_event = threading.Event()
    errors: dict[int, str] = {}
    by_n = {p.n: p for p in parts}
    job.parts = [{"n": p.n, "title": p.title, "state": "pending", "model": None} for p in parts]
    save_job(cfg, job)
    in_flight: dict[int, threading.Thread] = {}

    def set_state(n: int, state: str, model: str | None = None) -> None:
        with state_lock:
            _set_part(job, n, state, model)
            save_job(cfg, job)

    def run_part(part: PlanPart) -> None:
        model = build_model_for(cfg, part.judgment)
        set_state(part.n, "building", model)
        job.build_model = model
        try:
            result = _run_one_part(cfg, job, directory, job_wt, parts, part, plan_text, merge_lock)
        except JobFailed as exc:
            errors[part.n] = str(exc)
            fail_event.set()
            set_state(part.n, "failed")
            return
        except Exception as exc:  # noqa: BLE001
            errors[part.n] = str(exc)
            fail_event.set()
            set_state(part.n, "failed")
            return
        set_state(part.n, result)

    while True:
        with state_lock:
            pending = [p for p in parts if _part_state(job, p.n) == "pending"]
            done = _done_part_numbers(job)
        if not fail_event.is_set():
            ready = [p for p in pending if all(dep in done for dep in p.after)]
            while ready and len(in_flight) < cfg.limit_cursor:
                part = ready.pop(0)
                model = build_model_for(cfg, part.judgment)
                set_state(part.n, "building", model)
                thread = threading.Thread(target=run_part, args=(part,), name=f"part-{part.n}")
                in_flight[part.n] = thread
                thread.start()
        if in_flight:
            while in_flight:
                finished = False
                for n, thread in list(in_flight.items()):
                    thread.join(timeout=0.05)
                    if not thread.is_alive():
                        del in_flight[n]
                        finished = True
                        break
                if finished:
                    break
            continue
        if fail_event.is_set():
            break
        clashed = [by_n[int(item["n"])] for item in job.parts if item.get("state") == "clashed"]
        if clashed:
            for part in clashed:
                set_state(part.n, "building")
                _rebuild_part(cfg, job, directory, job_wt, parts, part, plan_text)
                set_state(part.n, "rebuilt")
            continue
        pending_now = [p for p in parts if _part_state(job, p.n) == "pending"]
        if pending_now:
            raise JobFailed("parts scheduler stuck waiting on dependencies")
        break

    for thread in in_flight.values():
        thread.join()
    if errors:
        n, msg = next(iter(sorted(errors.items())))
        raise JobFailed(f"part {n} ({by_n[n].title}) failed: {msg}")


def _investigate_model(cfg: Config, job: Job) -> str:
    """--deep forces Grok, --quick forces Composer; otherwise the task triage decides.

    The decision is saved on the job so a restart or a re-run picks the same model, and so
    the plan message can say why.
    """
    decision = job.flags.get("triage")
    if not isinstance(decision, dict):
        forced = job.flags.get("deep")
        if forced is None:
            result = triage_task(job.task, cfg.deep_threshold)
            deep, reasons, score = result.deep, list(result.reasons), result.score
            source = "auto"
        else:
            deep, reasons, score = bool(forced), [], 0
            source = "--deep" if forced else "--quick"
        decision = {"deep": deep, "score": score, "reasons": reasons, "source": source}
        job.flags["triage"] = decision
        save_job(cfg, job)
    return GROK_MODEL if decision.get("deep") else COMPOSER_MODEL


def process_code(cfg: Config, job: Job) -> None:
    directory = job_dir(cfg, job.id)
    if job.state == "queued":
        transition(cfg, job, "investigating")
    if job.state == "investigating":
        workspace = Path(job.repo).expanduser() if job.repo else directory
        code, out = investigate(
            cfg,
            directory,
            workspace,
            prompts.investigate_brief(job.task),
            model=_investigate_model(cfg, job),
        )
        if code != 0:
            raise JobFailed(f"investigate failed with exit {code}")
        directory.joinpath("evidence.md").write_text(out)
        transition(cfg, job, "planning")
    if job.state == "planning":
        plan_text = _run_plan(cfg, job)
        questions = parse_open_questions(plan_text)
        if questions:
            _stop(cfg, job, "needs-answer")
            return
        task_text = _read(directory / "task.md")
        plan_path = directory / "plan.md"
        directory.joinpath("plan-view.md").write_text(
            render_plan_view(
                job,
                task_text,
                plan_text,
                plan_path,
                max_parts=cfg.max_parts,
                cheap_model=build_model_for(cfg, "none"),
            )
        )
        skip_gate = (
            bool(job.flags.get("approved")) or bool(job.flags.get("auto")) or cfg.auto_approve
        )
        if not skip_gate:
            _stop(cfg, job, "needs-approval")
            return
        transition(cfg, job, "preparing")
    if job.state == "needs-approval":
        if not job.flags.get("approved"):
            return
        transition(cfg, job, "preparing")
    if job.state == "needs-answer":
        return
    plan_text = _read(directory / "plan.md")
    _size, judgment, verify_line = parse_plan_trailers(plan_text)
    if job.state == "preparing":
        git_prepare(cfg, job)
        transition(cfg, job, "building")
    if job.state in {"verifying", "reviewing", "fixing"} and not (
        job.worktree and Path(job.worktree).exists()
    ):
        git_prepare(cfg, job)
    repo = Path(job.worktree) if job.worktree else Path(job.repo) if job.repo else directory
    if job.state == "building":
        parts, split_note = parse_plan_parts(plan_text, max_parts=cfg.max_parts)
        job.split_note = split_note
        start_checklist(job, planned_files(plan_text))
        save_job(cfg, job)
        if len(parts) >= 2:
            _build_parts(cfg, job, directory, repo, parts, plan_text)
        else:
            model = build_model_for(cfg, judgment)
            job.build_model = model
            before = _head(repo)
            code = _build_watched(cfg, job, directory, repo, model, prompts.build_brief(plan_text))
            if code != 0:
                raise JobFailed(f"build failed with exit {code}")
            guard_branch(repo, job, before)
            commit_if_changes(repo, f"onyx-job {job.id}: building")
        save_job(cfg, job)
        transition(cfg, job, "verifying")
    only = job.flags.get("only")
    while job.state in {"verifying", "reviewing", "fixing"}:
        if job.state == "fixing":
            model = COMPOSER_MODEL
            job.build_model = model
            review_text = _read(directory / "review.md")
            verify_log = _read(directory / "verify.log")
            before = _head(repo)
            code = _build_watched(
                cfg,
                job,
                directory,
                repo,
                model,
                prompts.fix_brief(plan_text, review_text, verify_log),
                stage="fixing",
            )
            if code != 0:
                raise JobFailed(f"fix failed with exit {code}")
            job.fix_rounds_used += 1
            guard_branch(repo, job, before)
            commit_if_changes(repo, f"onyx-job {job.id}: fixing")
            save_job(cfg, job)
            transition(cfg, job, "verifying")
        if job.state == "verifying":
            _run_verify(cfg, job, verify_line)
            if only == "verify":
                if _verify_ok(job):
                    _stop(cfg, job, "done")
                else:
                    _stop(cfg, job, "failed", "verify failed (verify-only run)")
                return
            transition(cfg, job, "reviewing")
        if job.state == "reviewing":
            base = job.base_branch or "HEAD"
            prompt = prompts.review_prompt(plan_text, base, job.verify_results)
            code, out = review(cfg, directory, repo, prompt)
            if code != 0 or not out.strip():
                code, out = review(cfg, directory, repo, prompt)
            if code != 0 or not out.strip():
                raise JobFailed("review tool failed twice; see review.log")
            directory.joinpath("review.md").write_text(out)
            job.verdict = parse_verdict(out)
            save_job(cfg, job)
            if only == "review":
                if job.verdict == "PASS":
                    _stop(cfg, job, "done")
                else:
                    _stop(
                        cfg,
                        job,
                        "failed",
                        "review found problems (review-only run, no fix rounds); see review.md",
                    )
                return
            if _verify_ok(job) and job.verdict == "PASS":
                _stop(cfg, job, "done")
                return
            max_fixes = 2
            if job.fix_rounds_used >= max_fixes:
                _stop(cfg, job, "failed", "verify or review failed after fix rounds")
                return
            transition(cfg, job, "fixing")


def _research_report_path(cfg: Config, job: Job) -> Path:
    cfg.research_dir.mkdir(parents=True, exist_ok=True)
    from datetime import datetime

    day = datetime.now().strftime("%Y-%m-%d")
    slug = slugify_task(job.task)
    name = f"{day}-{slug}.md"
    path = cfg.research_dir / name
    n = 2
    while path.exists():
        path = cfg.research_dir / f"{day}-{slug}-{n}.md"
        n += 1
    return path


def process_research(cfg: Config, job: Job) -> None:
    directory = job_dir(cfg, job.id)
    if job.state == "queued":
        transition(cfg, job, "investigating")
    if job.state == "investigating":
        code, out = investigate(
            cfg,
            directory,
            directory,
            prompts.investigate_brief(job.task, research=True),
            timeout=cfg.research_timeout,
            model=_investigate_model(cfg, job),
        )
        if code != 0:
            raise JobFailed(f"investigate failed with exit {code}")
        directory.joinpath("evidence.md").write_text(out)
        transition(cfg, job, "writing")
    if job.state == "writing":
        prompt = prompts.research_report_prompt(
            _read(directory / "task.md"), _read(directory / "evidence.md")
        )
        code, out = plan_claude(
            cfg, directory, directory, prompt, REPORT_MODEL, timeout=cfg.research_timeout
        )
        if claude_capped_or_failed(code, out):
            code, out = plan_claude(
                cfg, directory, directory, prompt, REPORT_MODEL, timeout=cfg.research_timeout
            )
        if claude_capped_or_failed(code, out):
            job.planner = "grok-fallback"
            code, out = plan_grok(cfg, directory, directory, prompt, timeout=cfg.research_timeout)
            if code != 0 or not out.strip():
                raise JobFailed("research writing failed")
        else:
            job.planner = "claude"
        report_path = _research_report_path(cfg, job)
        report_path.write_text(out)
        directory.joinpath("report.md").write_text(out)
        job.report_path = str(report_path)
        save_job(cfg, job)
        transition(cfg, job, "fact-checking")
    if job.state == "fact-checking":
        report = Path(job.report_path) if job.report_path else directory / "report.md"
        if not report.exists():
            report = directory / "report.md"
        # A rerun starts from the report as written: drop the last run's dispute section.
        report_text = _read(report)
        cut = _FACT_CHECK_SECTION_RE.search(report_text)
        if cut:
            report.write_text(report_text[: cut.start()].rstrip() + "\n")
        prompt = prompts.fact_check_prompt(report)
        review_md = directory / "review.md"
        for _attempt in range(2):
            review_md.unlink(missing_ok=True)
            code, out = fact_check(cfg, directory, prompt)
            if not review_md.exists() or not review_md.read_text().strip():
                review_md.write_text(out)
            if code == 0 and review_md.read_text().strip():
                break
        else:
            raise JobFailed("fact-check failed twice; the report was not checked (see review.log)")
        review_text = review_md.read_text()
        job.verdict = parse_verdict(review_text, corrections=True)
        if job.verdict != "PASS" and job.report_path:
            report_file = Path(job.report_path)
            existing = report_file.read_text()
            if not _FACT_CHECK_SECTION_RE.search(existing):
                if job.verdict == "CORRECTIONS":
                    # The heading says it; a bare verdict line reads like a failed job.
                    notes = review_text.rstrip().rsplit("\n", 1)[0].rstrip()
                    section = "## Corrections from the fact-check\n\n" + notes + "\n"
                else:
                    section = "## Disputed\n\n" + review_text
                report_file.write_text(existing.rstrip() + "\n\n" + section)
        save_job(cfg, job)
        _stop(cfg, job, "done")


_OPS_JSON_FENCE_RE = re.compile(r"```json[ \t]*\r?\n.*?```", re.DOTALL)


def _ops_brief_parts(text: str) -> tuple[str, dict]:
    """Split a brief into the markdown body and the validated schema block."""
    brief = parse_brief(text)
    matches = list(_OPS_JSON_FENCE_RE.finditer(text))
    body = text[: matches[-1].start()].rstrip() if matches else text.rstrip()
    return body, brief


_OPS_DIGEST_SEP = b"\x00"


def _ops_brief_digest(cfg: Config, job: Job) -> str | None:
    """SHA-256 of brief.md and brief.json concatenated, or None if either is missing.

    Both files are covered, separated by a NUL byte, so a change to either the
    human-readable brief or the structured schema after approval is caught.
    """
    directory = job_dir(cfg, job.id)
    md_path = directory / "brief.md"
    json_path = directory / "brief.json"
    if not md_path.is_file() or not json_path.is_file():
        return None
    payload = md_path.read_bytes() + _OPS_DIGEST_SEP + json_path.read_bytes()
    return hashlib.sha256(payload).hexdigest()


_OPS_DELIVERY_BACKOFF = (60, 120, 240)


def _ops_wait_for_approval(cfg: Config, job: Job) -> None:
    """Deliver the approval request and record that it was sent.

    Without a delivered message the job stays unapprovable through the Telegram path:
    ``approve_job`` refuses while ``ops_approval_message_id`` is unset. Delivery is
    retried with an increasing delay; after four failures the job is failed.
    """
    if job.state != "needs-approval":
        transition(cfg, job, "needs-approval")
    text = compose_stop_message(cfg, job)
    silent = in_quiet_hours(datetime.now().astimezone(), cfg.quiet_hours)
    sent_id = None if cfg.no_deliver else send_message(cfg, text, silent=silent)
    if sent_id is not None:
        job.flags["ops_approval_message_id"] = sent_id
        job.flags["delivered"] = job.state
        job.flags["settled"] = job.state
        job.flags["ops_delivery_attempts"] = 0
        job.flags["ops_next_delivery_at"] = None
        save_job(cfg, job)
        return
    attempts = int(job.flags.get("ops_delivery_attempts") or 0) + 1
    job.flags["ops_delivery_attempts"] = attempts
    job.flags["settled"] = job.state
    if attempts > len(_OPS_DELIVERY_BACKOFF):
        save_job(cfg, job)
        _stop(cfg, job, "failed", "could not deliver the approval request")
        return
    job.flags["ops_next_delivery_at"] = time.time() + _OPS_DELIVERY_BACKOFF[attempts - 1]
    save_job(cfg, job)
    notify_job(cfg, job)


def _ops_briefing(cfg: Config, job: Job, directory: Path) -> None:
    # task.md carries revision feedback, so re-briefs see it; fall back to the
    # raw task if the file is missing.
    task = _read(directory / "task.md") or job.task
    code, out = brief_ops(cfg, directory, task)
    if code != 0 or not out.strip():
        raise JobFailed(f"operator briefing failed with exit {code}")
    try:
        body, brief = _ops_brief_parts(out)
    except ValueError as exc:
        raise JobFailed(f"operator brief is invalid: {exc}") from exc
    directory.joinpath("brief.md").write_text(body + "\n")
    payload = json.dumps(brief, indent=2) + "\n"
    directory.joinpath("brief.json").write_text(payload)
    job.flags["ops_brief_sha256"] = _ops_brief_digest(cfg, job)
    job.flags["ops_changes"] = list(brief["changes"])
    save_job(cfg, job)
    _ops_wait_for_approval(cfg, job)


def process_ops(cfg: Config, job: Job) -> None:
    directory = job_dir(cfg, job.id)
    if job.state == "queued":
        transition(cfg, job, "briefing")
    if job.state == "briefing":
        _ops_briefing(cfg, job, directory)
        return
    if job.state == "needs-approval":
        if not job.flags.get("approved"):
            if not job.flags.get("ops_approval_message_id"):
                # The first delivery failed; retry it so the job becomes approvable.
                # Approval itself stays blocked until a message id is recorded.
                _ops_wait_for_approval(cfg, job)
            return
        recorded = job.flags.get("ops_brief_sha256")
        if not recorded or recorded != _ops_brief_digest(cfg, job):
            raise JobFailed("operator brief changed after approval; refusing to operate")
        transition(cfg, job, "operating")
    if job.state == "operating":
        brief_path = directory / "brief.json"
        try:
            brief = json.loads(brief_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise JobFailed(f"operator brief is unreadable: {exc}") from exc
        code, out = operate_ops(cfg, directory, render_operator_brief(brief))
        if code != 0:
            report_file = directory / "report.md"
            report_file.write_text(f"Operator failed (exit {code})\n\n{out}")
            job.report_path = str(report_file)
            save_job(cfg, job)
            _stop(cfg, job, "failed", f"operator failed (exit {code})")
            return
        transition(cfg, job, "verifying")
    if job.state == "verifying":
        brief_path = directory / "brief.json"
        try:
            brief = json.loads(brief_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise JobFailed(f"operator brief is unreadable: {exc}") from exc
        code, report = verify_ops(cfg, directory, brief)
        directory.joinpath("report.md").write_text(report)
        report_path = _research_report_path(cfg, job)
        report_path.write_text(report)
        job.report_path = str(report_path)
        save_job(cfg, job)
        first = report.strip().splitlines()[0].strip() if report.strip() else ""
        if code == 0 and first == "RESULT: confirmed":
            _stop(cfg, job, "done")
        else:
            if code != 0:
                detail = f"verify exited {code}"
            else:
                detail = first.removeprefix("RESULT: ").strip() or "no verdict"
            _stop(cfg, job, "failed", f"ops not confirmed: {detail}")


def _job_is_waiting(job: Job) -> bool:
    """A stop state where the runner is waiting on David, not a failure to record.

    An ops job that David has approved is not waiting: a failure after approval must
    become a failed job, or the approval would silently do nothing.
    """
    if job.state in {"done", "failed", "cancelled"}:
        return False
    if job.state == "needs-answer":
        return True
    if job.state == "needs-approval":
        return not job.flags.get("approved")
    return False


def process_job(cfg: Config, job: Job) -> None:
    job.runner_pid = os.getpid()
    save_job(cfg, job)
    try:
        if job.kind == "ops":
            process_ops(cfg, job)
        elif job.kind == "research":
            process_research(cfg, job)
        else:
            process_code(cfg, job)
    except JobFailed as exc:
        current = load_job(cfg, job.id)
        if not _job_is_waiting(current):
            current.error = str(exc)
            _stop(cfg, current, "failed", str(exc))
    except Exception as exc:
        current = load_job(cfg, job.id)
        if not _job_is_waiting(current):
            _stop(cfg, current, "failed", str(exc))
    finally:
        current = load_job(cfg, job.id)
        current.runner_pid = None
        save_job(cfg, current)


def run_loop(cfg: Config | None = None) -> None:
    cfg = cfg or load_config()
    cfg.jobs_dir.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            with job_lock(cfg.jobs_dir, blocking=False):
                while True:
                    job = next_runnable(cfg)
                    if job is None:
                        break
                    process_job(cfg, job)
        except LockHeldError:
            return
        if next_runnable(cfg) is None:
            return


def spawn_runner(cfg: Config) -> None:
    if lock_held(cfg):
        return
    cfg.jobs_dir.mkdir(parents=True, exist_ok=True)
    log = open(cfg.jobs_dir / "runner.log", "a")  # noqa: SIM115
    try:
        subprocess.Popen(
            [sys.executable, "-m", "onyx_pipeline.cli", "_run"],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            close_fds=True,
        )
    finally:
        log.close()


def answer_job(cfg: Config, job_id: str, text: str) -> Job:
    job = load_job(cfg, job_id)
    job.flags["delivered"] = None
    path = job_dir(cfg, job.id) / "task.md"
    content = path.read_text() if path.exists() else ""
    if "## Answers" not in content:
        content = content.rstrip() + "\n\n## Answers\n"
    content = content.rstrip() + "\n" + text.rstrip() + "\n"
    path.write_text(content)
    job.flags["answer"] = text
    transition(cfg, job, "planning")
    return job


def approve_job(cfg: Config, job_id: str) -> Job:
    job = load_job(cfg, job_id)
    if job.state != "needs-approval":
        raise ValueError(f"job {job_id} is {job.state}, not waiting for approval")
    if job.kind == "ops":
        _require_ops_approvable(cfg, job)
    job.flags["delivered"] = None
    job.flags["approved"] = True
    save_job(cfg, job)
    return job


def _require_ops_approvable(cfg: Config, job: Job) -> None:
    """An ops job is only approvable if its request was delivered and its brief is intact."""
    if job.flags.get("ops_approval_message_id") is None:
        raise ValueError(f"job {job.id} approval was never delivered; cannot approve")
    recorded = job.flags.get("ops_brief_sha256")
    if not recorded or recorded != _ops_brief_digest(cfg, job):
        raise ValueError(f"job {job.id} brief changed since it was sent; cannot approve")


RERUN_STAGES: dict[str, dict[str, str]] = {
    "code": {"verify": "verifying", "review": "reviewing"},
    "research": {"fact-check": "fact-checking"},
}


def rerun_job(cfg: Config, job_id: str, stage: str, *, fix: bool = False) -> Job:
    job = load_job(cfg, job_id)
    if job.state not in {"done", "failed"}:
        raise ValueError(
            f"job {job_id} is {job.state}; only a finished job (done or failed) can re-run a stage"
        )
    allowed = RERUN_STAGES.get(job.kind, {})
    if stage not in allowed:
        raise ValueError(
            f"stage {stage} does not apply to a {job.kind} job; use one of: {', '.join(allowed)}"
        )
    if fix and stage != "review":
        raise ValueError("--fix only applies to --stage review")
    if job.kind == "code":
        plan_missing = not (job_dir(cfg, job.id) / "plan.md").exists()
        branch_ok = False
        if job.repo and job.branch:
            checked = _git(Path(job.repo), "rev-parse", "--verify", job.branch, check=False)
            branch_ok = checked.returncode == 0
        # The branch exists from `preparing` on; only a finished build reaches `verifying`.
        built = any(item.get("state") == "verifying" for item in job.stages)
        if plan_missing or not job.repo or not job.branch or not branch_ok or not built:
            raise ValueError(
                f"job {job_id} was never built (no finished build); start a new job instead"
            )
    else:
        report = Path(job.report_path) if job.report_path else None
        local = job_dir(cfg, job.id) / "report.md"
        if not ((report is not None and report.exists()) or local.exists()):
            raise ValueError(f"job {job_id} has no report to check")
    job.error = None
    job.verdict = None
    job.flags["only"] = None if fix else stage
    if fix:
        job.fix_rounds_used = 0
    transition(cfg, job, RERUN_STAGES[job.kind][stage])
    return job


def revise_job(cfg: Config, job_id: str, text: str) -> Job:
    job = load_job(cfg, job_id)
    if job.state != "needs-approval":
        raise ValueError(f"job {job_id} is {job.state}, not waiting for approval")
    job.flags["delivered"] = None
    revisions = int(job.flags.get("revisions") or 0)
    if revisions >= 3:
        raise ValueError("revision limit reached: approve or cancel")
    path = job_dir(cfg, job.id) / "task.md"
    content = path.read_text() if path.exists() else ""
    round_n = revisions + 1
    content = content.rstrip() + f"\n## Plan feedback (round {round_n})\n{text.rstrip()}\n"
    path.write_text(content)
    job.flags["revisions"] = round_n
    job.flags["approved"] = False
    if job.kind == "ops":
        # An ops job has no plan: drop the approved brief and its delivery state so
        # process_ops briefs again from the feedback now in task.md.
        for name in ("brief.md", "brief.json"):
            (job_dir(cfg, job.id) / name).unlink(missing_ok=True)
        job.flags["ops_brief_sha256"] = None
        job.flags["ops_changes"] = None
        job.flags["ops_approval_message_id"] = None
        job.flags["ops_delivery_attempts"] = 0
        job.flags["ops_next_delivery_at"] = None
        transition(cfg, job, "queued")
        return job
    transition(cfg, job, "planning")
    return job


def cancel_job(cfg: Config, job_id: str) -> Job:
    job = load_job(cfg, job_id)
    pid = job.runner_pid
    job.error = job.error or "cancelled"
    if pid and pid != os.getpid():
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except OSError:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
    job.runner_pid = None
    kill_active_workers(job_dir(cfg, job.id))
    _stop(cfg, job, "cancelled")
    return job
