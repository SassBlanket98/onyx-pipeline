"""Subprocess wrappers for worker CLIs."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from onyx_pipeline.config import Config
from onyx_pipeline.prompts import ops_brief_prompt, ops_operate_prompt, ops_verify_prompt

STALLED = 125  # exit code for a worker killed by the watchdog


@dataclass
class Watch:
    repo: Path
    base: str
    idle_limit: int
    poll: float
    on_tick: Callable[[list[str]], None] | None = None


COMPOSER_MODEL = "composer-2.5-fast"
GROK_MODEL = "cursor-grok-4.6-high"
# Plans and reports both run on Sonnet to stretch David's Claude usage. Plans are
# instructions other agents follow, so it is the model to move back to Opus if they slip.
PLAN_MODEL = ("claude-sonnet-5-5", "medium")
REPORT_MODEL = ("claude-sonnet-5-5", "high")
# The operator brief is an instruction other agents follow, so it runs like a plan.
OPS_BRIEF_MODEL = ("claude-sonnet-5-5", "medium")
# Operating and verifying both run on Sonnet: the brief is a plan another agent follows.
OPS_MODEL = "claude-sonnet-5-5"
# Chrome is driven through Claude-in-Chrome; nothing else is allowed to run.
OPS_CHROME_TOOLS = "mcp__claude-in-chrome__*"
# Verification is read-only: DNS and HTTP lookups, plus reading the brief's files.
OPS_VERIFY_TOOLS = "Read,Bash(dig *),Bash(curl -sI *)"

# The user's CLAUDE.md still loads in headless runs (only --bare skips it, and --bare
# can't use subscription login), so state plainly that its working rules don't apply.
PLANNER_SYSTEM = (
    "You are a non-interactive worker in an automated pipeline. Any instructions from "
    "CLAUDE.md or memory about delegating work, routing to Cursor or Codex, asking the "
    "user questions, or doing the work yourself do not apply to this run. You cannot "
    "edit files. Your only output is the document the prompt asks for, in the exact "
    "format it asks for."
)


def changed_paths(repo: Path, base: str) -> list[str]:
    """Repo-relative paths changed since `base`, or [] if git fails."""
    paths: set[str] = set()
    try:
        status = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain", "-uall"],
            capture_output=True,
            text=True,
            check=False,
        )
        if status.returncode != 0:
            return []
        for line in status.stdout.splitlines():
            if len(line) < 4:
                continue
            entry = line[3:].strip()
            if " -> " in entry:
                entry = entry.split(" -> ", 1)[1]
            entry = entry.strip().strip('"')
            if entry:
                paths.add(entry)
        diff = subprocess.run(
            ["git", "-C", str(repo), "diff", "--name-only", base, "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        if diff.returncode != 0:
            return []
        for line in diff.stdout.splitlines():
            line = line.strip()
            if line:
                paths.add(line)
    except (OSError, ValueError):
        return []
    return sorted(paths)


def _max_mtime(repo: Path, paths: list[str]) -> float:
    best = 0.0
    for rel in paths:
        path = repo / rel
        try:
            if path.is_file():
                best = max(best, path.stat().st_mtime)
        except OSError:
            continue
    return best


def _activity_signature(
    repo: Path, base: str, stdout_path: Path, stderr_path: Path
) -> tuple[tuple[str, ...], float, int]:
    paths = changed_paths(repo, base)
    try:
        out_size = stdout_path.stat().st_size if stdout_path.exists() else 0
        err_size = stderr_path.stat().st_size if stderr_path.exists() else 0
    except OSError:
        out_size, err_size = 0, 0
    return (tuple(paths), _max_mtime(repo, paths), out_size + err_size)


def run_capture(
    cmd: list[str],
    *,
    cwd: Path,
    timeout: int,
    pid_file: Path | None = None,
    watch: Watch | None = None,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    """Run a command with no stdin, in its own process group; return (code, stdout, stderr).

    On timeout the whole group is killed, so grandchildren that hold the output
    pipes open (node workers, app servers, dev servers) can't hang the pipeline.
    ``env`` overrides add to the inherited environment rather than replacing it.
    """
    child_env = {**os.environ, "PWD": str(cwd), **(env or {})}
    if watch is None:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        if pid_file is not None:
            pid_file.write_text(str(proc.pid))
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            kill_group(proc)
            stdout, stderr = proc.communicate()
            stderr = f"{stderr or ''}\n[onyx-job] killed after {timeout}s timeout"
            code = 124
        finally:
            kill_group(proc)
            if pid_file is not None:
                pid_file.unlink(missing_ok=True)
        return code, stdout or "", stderr or ""

    tmp_dir = Path(tempfile.mkdtemp())
    stdout_path = tmp_dir / "stdout"
    stderr_path = tmp_dir / "stderr"
    code = 1
    stdout = ""
    stderr = ""
    proc: subprocess.Popen[str] | None = None
    try:
        with (
            stdout_path.open("w", encoding="utf-8") as stdout_fh,
            stderr_path.open("w", encoding="utf-8") as stderr_fh,
        ):
            proc = subprocess.Popen(
                cmd,
                cwd=str(cwd),
                env=child_env,
                stdin=subprocess.DEVNULL,
                stdout=stdout_fh,
                stderr=stderr_fh,
                text=True,
                start_new_session=True,
            )
            if pid_file is not None:
                pid_file.write_text(str(proc.pid))
            started = time.monotonic()
            last_activity = started
            last_sig = _activity_signature(watch.repo, watch.base, stdout_path, stderr_path)
            last_paths: tuple[str, ...] = last_sig[0]
            timed_out = False
            stalled = False
            while True:
                try:
                    proc.wait(timeout=watch.poll)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if time.monotonic() - started > timeout:
                    kill_group(proc)
                    proc.wait()
                    stderr = stderr_path.read_text(encoding="utf-8")
                    stderr = f"{stderr}\n[onyx-job] killed after {timeout}s timeout"
                    code = 124
                    timed_out = True
                    break
                sig = _activity_signature(watch.repo, watch.base, stdout_path, stderr_path)
                if sig != last_sig:
                    last_activity = time.monotonic()
                    if sig[0] != last_paths and watch.on_tick is not None:
                        try:
                            watch.on_tick(list(sig[0]))
                        except Exception:
                            pass
                    last_paths = sig[0]
                    last_sig = sig
                if (
                    not timed_out
                    and watch.idle_limit > 0
                    and time.monotonic() - last_activity > watch.idle_limit
                ):
                    kill_group(proc)
                    proc.wait()
                    stderr = stderr_path.read_text(encoding="utf-8")
                    stderr = (
                        f"{stderr}\n[onyx-job] killed by the watchdog: no file changes or "
                        f"output for {watch.idle_limit}s"
                    )
                    code = STALLED
                    stalled = True
                    break
            if not timed_out and not stalled:
                code = proc.returncode or 0
            if watch.on_tick is not None:
                try:
                    watch.on_tick(changed_paths(watch.repo, watch.base))
                except Exception:
                    pass
            stdout = stdout_path.read_text(encoding="utf-8")
            if not timed_out and not stalled:
                stderr = stderr_path.read_text(encoding="utf-8")
    finally:
        if proc is not None:
            kill_group(proc)
        if pid_file is not None:
            pid_file.unlink(missing_ok=True)
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return code, stdout or "", stderr or ""


def kill_active_workers(job_directory: Path) -> None:
    """Terminate the process group of every worker still running for a job."""
    for pid_file in job_directory.glob("*.pgid"):
        try:
            pgid = int(pid_file.read_text().strip())
            os.killpg(pgid, signal.SIGTERM)
        except (ValueError, ProcessLookupError, PermissionError):
            pass
        pid_file.unlink(missing_ok=True)


def run_worker(
    cmd: list[str],
    *,
    cwd: Path,
    timeout: int,
    log_path: Path,
    watch: Watch | None = None,
    env: dict[str, str] | None = None,
) -> tuple[int, str]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    pid_file = log_path.with_name(log_path.name + ".pgid")
    code, stdout, stderr = run_capture(
        cmd, cwd=cwd, timeout=timeout, pid_file=pid_file, watch=watch, env=env
    )
    with log_path.open("a") as log:
        log.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} exit {code} =====\n")
        log.write(_combine_output(stdout, stderr) + "\n")
    return code, stdout


def kill_group(proc: subprocess.Popen[str], grace: float = 10.0) -> None:
    """SIGTERM the process group, then SIGKILL whatever is left after the grace period."""
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            continue
        # Leader gone; give stragglers in the group the same grace, then move on.
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.2)


def _combine_output(stdout: str, stderr: str) -> str:
    if stderr:
        return f"{stdout}\n{stderr}" if stdout else stderr
    return stdout


def investigate(
    cfg: Config,
    job_directory: Path,
    workspace: Path,
    brief: str,
    timeout: int | None = None,
    model: str = COMPOSER_MODEL,
) -> tuple[int, str]:
    cmd = [
        cfg.cursor_bin,
        "-p",
        "--trust",
        "--mode",
        "ask",
        "--model",
        model,
        "--workspace",
        str(workspace),
        "--output-format",
        "text",
        brief,
    ]
    return run_worker(
        cmd,
        cwd=workspace,
        timeout=timeout or cfg.worker_timeout,
        log_path=job_directory / "investigate.log",
    )


def plan_claude(
    cfg: Config,
    job_directory: Path,
    cwd: Path,
    prompt: str,
    model: tuple[str, str] = PLAN_MODEL,
    timeout: int | None = None,
) -> tuple[int, str]:
    cmd = [
        cfg.claude_bin,
        "-p",
        "--model",
        model[0],
        "--effort",
        model[1],
        "--allowedTools",
        "Read,Grep,Glob",
        "--disallowedTools",
        "Edit,Write,Bash,NotebookEdit",
        "--output-format",
        "text",
        "--add-dir",
        str(job_directory),
        "--setting-sources",
        "project",
        "--append-system-prompt",
        PLANNER_SYSTEM,
        prompt,
    ]
    return run_worker(
        cmd,
        cwd=cwd,
        timeout=timeout or cfg.worker_timeout,
        log_path=job_directory / "plan.log",
    )


def brief_ops(
    cfg: Config,
    job_directory: Path,
    task: str,
    timeout: int | None = None,
) -> tuple[int, str]:
    """Draft the operator brief for a raw request. Read-only: the worker can only
    read, and its output is the brief body plus one json block the caller validates."""
    cmd = [
        cfg.claude_bin,
        "-p",
        "--model",
        OPS_BRIEF_MODEL[0],
        "--effort",
        OPS_BRIEF_MODEL[1],
        "--allowedTools",
        "Read,Grep,Glob",
        "--disallowedTools",
        "Edit,Write,Bash,NotebookEdit",
        "--output-format",
        "text",
        "--add-dir",
        str(job_directory),
        "--setting-sources",
        "project",
        "--append-system-prompt",
        PLANNER_SYSTEM,
        ops_brief_prompt(task),
    ]
    return run_worker(
        cmd,
        cwd=job_directory,
        timeout=timeout or cfg.worker_timeout,
        log_path=job_directory / "brief.log",
    )


def ops_work_env() -> dict[str, str]:
    """Operate and verify share one dedicated Claude config dir, kept out of ~/.claude."""
    return {"CLAUDE_CONFIG_DIR": str(Path.home() / ".claude-work")}


def operate_ops(
    cfg: Config,
    job_directory: Path,
    brief_md: str,
    timeout: int | None = None,
) -> tuple[int, str]:
    """Carry out an approved brief in Chrome: the operator gets the brief as its
    prompt and the Chrome tools, and nothing else may run."""
    cmd = [
        cfg.claude_bin,
        "--chrome",
        "--model",
        OPS_MODEL,
        f"--allowedTools={OPS_CHROME_TOOLS}",
        "-p",
        ops_operate_prompt(brief_md),
    ]
    return run_worker(
        cmd,
        cwd=job_directory,
        timeout=timeout or cfg.worker_timeout,
        log_path=job_directory / "operate.log",
        env=ops_work_env(),
    )


_VERIFY_VERDICTS = ("confirmed", "uncertain", "failed")
_RESULT_LINES = frozenset(f"RESULT: {verdict}" for verdict in _VERIFY_VERDICTS)


def _verify_report(exit_code: int, output: str) -> str:
    """Return the report with a valid RESULT first line, even when the verifier failed.

    An explicit ``RESULT:`` line is trusted. A run that produced no verdict is
    ``uncertain`` when it exited cleanly and ``failed`` when it did not, so a
    crash never reads as a confirmation.
    """
    text = (output or "").strip()
    first_line = text.splitlines()[0].strip() if text else ""
    if first_line in _RESULT_LINES:
        return text
    verdict = "failed" if exit_code != 0 else "uncertain"
    reason = (
        f"the verifier exited {exit_code} without a result"
        if exit_code != 0
        else "the verifier did not report a result"
    )
    report = f"RESULT: {verdict}\n\n{reason}.\n"
    if text:
        report += f"\n{text}\n"
    return report


def verify_ops(
    cfg: Config,
    job_directory: Path,
    brief: dict,
    timeout: int | None = None,
) -> tuple[int, str]:
    """Check the operator's changes read-only and return a report whose first line
    is ``RESULT: confirmed|uncertain|failed``."""
    cmd = [
        cfg.claude_bin,
        "-p",
        "--model",
        OPS_MODEL,
        f"--allowedTools={OPS_VERIFY_TOOLS}",
        ops_verify_prompt(brief),
    ]
    code, out = run_worker(
        cmd,
        cwd=job_directory,
        timeout=timeout or cfg.verify_timeout,
        log_path=job_directory / "verify.log",
        env=ops_work_env(),
    )
    return code, _verify_report(code, out)


def think(cfg: Config, log_path: Path, workspace: Path, prompt: str) -> tuple[int, str]:
    """Read-only thinker run: Grok through Cursor. Grok is never used on the OpenCode
    plan; ONYX_THINK_BACKEND=opencode is for a cheap model set in ONYX_THINK_MODEL."""
    if cfg.think_backend != "opencode" or "grok" in cfg.think_model.lower():
        cmd = [
            cfg.cursor_bin,
            "-p",
            "--trust",
            "--mode",
            "ask",
            "--model",
            GROK_MODEL,
            "--workspace",
            str(workspace),
            "--output-format",
            "text",
            prompt,
        ]
    else:
        cmd = [
            cfg.opencode_bin,
            "run",
            "--standalone",
            "-m",
            cfg.think_model,
            "--agent",
            "plan",
            prompt,
        ]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    code, stdout, stderr = run_capture(cmd, cwd=workspace, timeout=cfg.think_timeout)
    log_path.write_text(_combine_output(stdout, stderr))
    return code, stdout


def plan_grok(
    cfg: Config,
    job_directory: Path,
    workspace: Path,
    prompt: str,
    timeout: int | None = None,
) -> tuple[int, str]:
    cmd = [
        cfg.cursor_bin,
        "-p",
        "--trust",
        "--mode",
        "ask",
        "--model",
        GROK_MODEL,
        "--workspace",
        str(workspace),
        "--output-format",
        "text",
        prompt,
    ]
    return run_worker(
        cmd,
        cwd=workspace,
        timeout=timeout or cfg.worker_timeout,
        log_path=job_directory / "plan.log",
    )


def build_model_for(cfg: Config, judgment: str) -> str:
    """Who builds a plan part: Grok through Cursor when the plan says judgment is needed,
    otherwise the cheap OpenCode worker (or Composer with ONYX_BUILD_BACKEND=cursor).
    Grok is never run through OpenCode."""
    if judgment == "needed":
        return GROK_MODEL
    if cfg.build_backend != "opencode" or "grok" in cfg.build_model.lower():
        return COMPOSER_MODEL
    return cfg.build_model


def builder_label(model: str) -> str:
    if model == GROK_MODEL:
        return "Grok 4.6 high (judgment needed)"
    if model == COMPOSER_MODEL:
        return "Composer 2.5"
    return model.split("/", 1)[-1].split("#", 1)[0] + " (OpenCode)"


def build(
    cfg: Config,
    job_directory: Path,
    repo: Path,
    model: str,
    brief: str,
    stage: str = "building",
    log_name: str | None = None,
    watch: Watch | None = None,
) -> tuple[int, str]:
    if model.startswith("opencode"):
        # --auto lets it run commands unattended, like Cursor's --force below.
        # --standalone: without it the session lives in OpenCode's background service
        # and keeps running (and billing) after this process is killed.
        cmd = [cfg.opencode_bin, "run", "--standalone", "-m", model, "--auto", brief]
    else:
        cmd = [
            cfg.cursor_bin,
            "-p",
            "--trust",
            "--force",
            "--model",
            model,
            "--workspace",
            str(repo),
            "--output-format",
            "text",
            brief,
        ]
    if log_name is None:
        log_name = "build.log" if stage == "building" else "fixing.log"
    return run_worker(
        cmd,
        cwd=repo,
        timeout=cfg.worker_timeout,
        log_path=job_directory / log_name,
        watch=watch,
    )


def review(cfg: Config, job_directory: Path, repo: Path, prompt: str) -> tuple[int, str]:
    """Read-only Codex review. `codex review --base` can't take a prompt, so use exec."""
    review_md = job_directory / "review.md"
    review_md.unlink(missing_ok=True)
    cmd = [
        cfg.codex_bin,
        "exec",
        "-s",
        "read-only",
        "-C",
        str(repo),
        "-o",
        str(review_md),
        prompt,
    ]
    code, out = run_worker(
        cmd,
        cwd=repo,
        timeout=cfg.worker_timeout,
        log_path=job_directory / "review.log",
    )
    text = review_md.read_text() if review_md.exists() else ""
    return code, text or out


def fact_check(cfg: Config, job_directory: Path, prompt: str) -> tuple[int, str]:
    review_md = job_directory / "review.md"
    cmd = [
        cfg.codex_bin,
        "exec",
        "-s",
        "read-only",
        "--skip-git-repo-check",
        "-C",
        str(job_directory),
        "-o",
        str(review_md),
        prompt,
    ]
    return run_worker(
        cmd,
        cwd=job_directory,
        timeout=cfg.worker_timeout,
        log_path=job_directory / "fact-checking.log",
    )


def notify_command(cfg: Config, text: str, log_path: Path) -> tuple[int, str]:
    cmd = [
        cfg.openclaw_bin,
        "agent",
        "--agent",
        "onyx",
        "--session-key",
        cfg.notify_session,
        "--deliver",
        "--reply-channel",
        "telegram",
        "--reply-account",
        cfg.notify_account,
        *(["--reply-to", cfg.notify_to] if cfg.notify_to else []),
        *(["--model", cfg.notify_model] if cfg.notify_model else []),
        "-m",
        text,
    ]
    return run_worker(
        cmd,
        cwd=log_path.parent,
        timeout=cfg.worker_timeout,
        log_path=log_path,
    )
