"""Job folders, state.json, queue, and the jobs-dir lock."""

from __future__ import annotations

import fcntl
import json
import os
import re
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from onyx_pipeline.config import Config

STATES = (
    "queued",
    "investigating",
    "planning",
    "briefing",
    "needs-answer",
    "needs-approval",
    "preparing",
    "building",
    "operating",
    "verifying",
    "reviewing",
    "fixing",
    "writing",
    "fact-checking",
    "done",
    "failed",
    "cancelled",
)

TERMINAL_STATES = frozenset({"done", "failed", "cancelled"})

_WORD_RE = re.compile(r"[A-Za-z0-9]+")

DEFAULT_FLAGS: dict[str, object] = {
    "approved": False,
    "answer": None,
    "auto": False,
    "deep": None,
    "triage": None,
    "auto_quote": None,
    "revisions": 0,
    "delivered": None,
    "settled": None,
    "only": None,
    # ops jobs (raw Telegram text run in Chrome behind an explicit approval)
    "ops_brief_sha256": None,
    "ops_changes": None,
    "ops_approval_message_id": None,
}


class LockHeldError(Exception):
    """Another runner holds the jobs lock."""


@dataclass
class Job:
    id: str
    kind: str
    repo: str | None
    task: str
    state: str
    stages: list[dict[str, str]]
    runner_pid: int | None = None
    base_branch: str | None = None
    branch: str | None = None
    planner: str | None = None
    build_model: str | None = None
    fix_rounds_used: int = 0
    verify_results: list[dict[str, object]] = field(default_factory=list)
    verdict: str | None = None
    flags: dict[str, object] = field(default_factory=lambda: dict(DEFAULT_FLAGS))
    error: str | None = None
    report_path: str | None = None
    worktree: str | None = None
    split_note: str | None = None
    parts: list[dict[str, object]] = field(default_factory=list)
    request_id: str | None = None

    def display_state(self) -> str:
        if self.state == "building" and self.parts:
            done = sum(1 for p in self.parts if p.get("state") in {"merged", "rebuilt"})
            return f"building (parts {done}/{len(self.parts)} merged)"
        return self.state

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Job:
        raw_flags = data.get("flags")
        if not isinstance(raw_flags, dict):
            raw_flags = {}
        flags = {**DEFAULT_FLAGS, **raw_flags}
        return cls(
            id=str(data["id"]),
            kind=str(data["kind"]),
            repo=data.get("repo") if isinstance(data.get("repo"), str) else None,
            task=str(data["task"]),
            state=str(data["state"]),
            stages=list(data.get("stages") or []),  # type: ignore[arg-type]
            runner_pid=data.get("runner_pid") if isinstance(data.get("runner_pid"), int) else None,
            base_branch=data.get("base_branch")
            if isinstance(data.get("base_branch"), str)
            else None,
            branch=data.get("branch") if isinstance(data.get("branch"), str) else None,
            planner=data.get("planner") if isinstance(data.get("planner"), str) else None,
            build_model=data.get("build_model")
            if isinstance(data.get("build_model"), str)
            else None,
            fix_rounds_used=int(data.get("fix_rounds_used") or 0),
            verify_results=list(data.get("verify_results") or []),  # type: ignore[arg-type]
            verdict=data.get("verdict") if isinstance(data.get("verdict"), str) else None,
            flags=flags,
            error=data.get("error") if isinstance(data.get("error"), str) else None,
            report_path=data.get("report_path")
            if isinstance(data.get("report_path"), str)
            else None,
            worktree=data.get("worktree") if isinstance(data.get("worktree"), str) else None,
            split_note=data.get("split_note") if isinstance(data.get("split_note"), str) else None,
            parts=list(data.get("parts") or []),  # type: ignore[arg-type]
            request_id=data.get("request_id") if isinstance(data.get("request_id"), str) else None,
        )


def slugify_task(task: str, n_words: int = 5) -> str:
    words = _WORD_RE.findall(task.lower())[:n_words]
    return "-".join(words) or "job"


def make_job_id(task: str, jobs_dir: Path, now: datetime | None = None) -> str:
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    slug = slugify_task(task)
    job_id = f"{stamp}-{slug}"
    if not (jobs_dir / job_id).exists():
        return job_id
    n = 2
    while (jobs_dir / f"{job_id}-{n}").exists():
        n += 1
    return f"{job_id}-{n}"


def job_dir(cfg: Config, job_id: str) -> Path:
    return cfg.jobs_dir / job_id


def now_iso() -> str:
    return datetime.now().astimezone().isoformat()


# Parallel parts and the watchdog's checklist ticks save the same job from several
# threads; they share one tmp path, so saves and checklist edits take this lock.
SAVE_LOCK = threading.RLock()


def save_job(cfg: Config, job: Job) -> None:
    directory = job_dir(cfg, job.id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "state.json"
    tmp = directory / "state.json.tmp"
    with SAVE_LOCK:
        tmp.write_text(json.dumps(job.to_dict(), indent=2) + "\n")
        os.replace(tmp, path)


def load_job(cfg: Config, job_id: str) -> Job:
    path = job_dir(cfg, job_id) / "state.json"
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"invalid state.json for {job_id}")
    return Job.from_dict(data)


def list_jobs(cfg: Config) -> list[Job]:
    if not cfg.jobs_dir.exists():
        return []
    jobs: list[Job] = []
    for path in cfg.jobs_dir.iterdir():
        if (path / "state.json").is_file():
            jobs.append(load_job(cfg, path.name))
    jobs.sort(key=_queue_key)
    return jobs


def _queue_key(job: Job) -> tuple[str, str]:
    created = job.stages[0]["at"] if job.stages else job.id
    return (created, job.id)


def is_runnable(job: Job) -> bool:
    if job.state in TERMINAL_STATES:
        return False
    if job.state == "needs-answer":
        return False
    if job.state == "needs-approval":
        if job.kind == "ops" and job.flags.get("ops_approval_message_id") is None:
            # The approval message never went out; let the runner retry delivery.
            # Approval stays impossible until a message id is recorded.
            next_at = job.flags.get("ops_next_delivery_at")
            if next_at is None:
                return True
            return time.time() >= float(next_at)  # type: ignore[arg-type]
        return bool(job.flags.get("approved"))
    return True


def next_runnable(cfg: Config) -> Job | None:
    for job in list_jobs(cfg):
        if is_runnable(job):
            return job
    return None


def transition(cfg: Config, job: Job, state: str) -> None:
    if state not in STATES:
        raise ValueError(f"unknown state: {state}")
    from onyx_pipeline.wait import STOP_STATES

    if job.state in STOP_STATES and state not in STOP_STATES:
        job.flags["delivered"] = None
        job.flags["settled"] = None
    job.state = state
    job.stages.append({"state": state, "at": now_iso()})
    save_job(cfg, job)
    from onyx_pipeline.progress import update_progress

    update_progress(cfg, job)


def create_job(
    cfg: Config,
    *,
    kind: str,
    task: str,
    repo: str | None,
    auto: bool = False,
    auto_quote: str | None = None,
    deep: bool | None = None,
    request_id: str | None = None,
) -> Job:
    cfg.jobs_dir.mkdir(parents=True, exist_ok=True)
    job_id = make_job_id(task, cfg.jobs_dir)
    created = now_iso()
    flags = dict(DEFAULT_FLAGS)
    flags["auto"] = auto
    flags["deep"] = deep
    if auto_quote:
        flags["auto_quote"] = auto_quote
    job = Job(
        id=job_id,
        kind=kind,
        repo=repo,
        task=task,
        state="queued",
        stages=[{"state": "queued", "at": created}],
        flags=flags,
        request_id=request_id,
    )
    save_job(cfg, job)
    job_dir(cfg, job_id).joinpath("task.md").write_text(f"# Task\n\n{task}\n")
    return job


# Request-ID starts take their own lock file so the critical section never waits
# behind a runner, which holds the main jobs lock for a whole job.
REQUEST_LOCK_NAME = ".requests.lock"


def find_job_by_request_id(cfg: Config, request_id: str | None) -> Job | None:
    """Return the job reserved for ``request_id``, or None if there is none."""
    if not request_id or not cfg.jobs_dir.exists():
        return None
    for path in sorted(cfg.jobs_dir.iterdir()):
        if not (path / "state.json").is_file():
            continue
        try:
            job = load_job(cfg, path.name)
        except (OSError, ValueError):
            continue
        if job.request_id == request_id:
            return job
    return None


def create_job_idempotent(
    cfg: Config,
    *,
    request_id: str,
    kind: str,
    task: str,
    repo: str | None,
    auto: bool = False,
    auto_quote: str | None = None,
    deep: bool | None = None,
) -> tuple[Job, bool]:
    """Create a job for ``request_id``, or return the one already reserved.

    Returns ``(job, created)``; ``created`` is False for a repeat start with the
    same request id, so the caller must not spawn another runner.
    """
    cfg.jobs_dir.mkdir(parents=True, exist_ok=True)
    with job_lock(cfg.jobs_dir, blocking=True, name=REQUEST_LOCK_NAME):
        existing = find_job_by_request_id(cfg, request_id)
        if existing is not None:
            return existing, False
        job = create_job(
            cfg,
            kind=kind,
            task=task,
            repo=repo,
            auto=auto,
            auto_quote=auto_quote,
            deep=deep,
            request_id=request_id,
        )
        return job, True


@contextmanager
def job_lock(jobs_dir: Path, blocking: bool = False, *, name: str = ".lock") -> Iterator[int]:
    jobs_dir.mkdir(parents=True, exist_ok=True)
    path = jobs_dir / name
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
    try:
        fcntl.flock(fd, flags)
    except BlockingIOError as exc:
        os.close(fd)
        raise LockHeldError from exc
    try:
        yield fd
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def lock_held(cfg: Config) -> bool:
    try:
        with job_lock(cfg.jobs_dir, blocking=False):
            return False
    except LockHeldError:
        return True
