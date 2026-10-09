"""Notifications via openclaw, with quiet-hours delay."""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from onyx_pipeline.config import Config
from onyx_pipeline.jobs import Job, job_dir
from onyx_pipeline.workers import notify_command


def in_quiet_hours(now: datetime, quiet: tuple[int, int]) -> bool:
    start, end = quiet
    minutes = now.hour * 60 + now.minute
    start_m = start * 60
    end_m = end * 60
    if start_m == end_m:
        return False
    if start_m < end_m:
        return start_m <= minutes < end_m
    return minutes >= start_m or minutes < end_m


def seconds_until_quiet_end(now: datetime, quiet: tuple[int, int]) -> int:
    """Seconds to sleep until quiet hours end, or 0 if not in quiet hours."""
    if not in_quiet_hours(now, quiet):
        return 0
    _, end_hour = quiet
    end = now.replace(hour=end_hour, minute=0, second=0, microsecond=0)
    if end <= now:
        end += timedelta(days=1)
    return int((end - now).total_seconds())


LONG_JOB_SECONDS = 3600


def ran_long(job: Job, now: datetime) -> bool:
    """Only long (overnight-style) jobs wait for quiet hours to end.

    A short job started at night means David is awake and waiting for it.
    """
    if not job.stages:
        return True
    try:
        started = datetime.fromisoformat(str(job.stages[0]["at"]))
    except ValueError:
        return True
    if started.tzinfo is None:
        started = started.astimezone()
    return (now - started).total_seconds() > LONG_JOB_SECONDS


def notify_message(job: Job) -> str:
    return (
        f"[onyx-job {job.id} {job.state}] "
        f"Run: onyx-job summary {job.id}, then tell David in your own words."
    )


def notify_job(cfg: Config, job: Job, *, sleep_fn: Callable[[float], None] | None = None) -> None:
    text = notify_message(job)
    log_path = job_dir(cfg, job.id) / "notify.log"
    if cfg.no_notify or os.environ.get("OPENCLAW_SHELL") == "exec":
        # Started from an agent's exec: OpenClaw forbids messaging sessions from there.
        # The agent gets the result from `onyx-job wait` exiting instead.
        with log_path.open("a") as fh:
            fh.write(f"{text}\n(not sent: result returns via onyx-job wait)\n")
        return
    now = datetime.now().astimezone()
    delay = 0
    if job.state != "failed" and ran_long(job, now):
        delay = seconds_until_quiet_end(now, cfg.quiet_hours)
    if delay > 0 and sleep_fn is None:
        # Hold the message, not the runner: a detached process sends it later.
        notify_later(cfg, text, log_path, delay)
        return
    if delay > 0 and sleep_fn is not None:
        sleep_fn(delay)
    notify_command(cfg, text, log_path)


def notify_later(cfg: Config, text: str, log_path: Path, delay: int) -> None:
    """Send the notification after `delay` seconds from a detached process."""
    import subprocess
    import sys

    code = (
        "import time, sys\n"
        "from onyx_pipeline.config import load_config\n"
        "from onyx_pipeline.workers import notify_command\n"
        "from pathlib import Path\n"
        f"time.sleep({int(delay)})\n"
        "notify_command(load_config(), sys.argv[1], Path(sys.argv[2]))\n"
    )
    with log_path.open("a") as fh:
        fh.write(f"held for quiet hours ({delay}s): {text}\n")
    subprocess.Popen(
        [sys.executable, "-c", code, text, str(log_path)],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
