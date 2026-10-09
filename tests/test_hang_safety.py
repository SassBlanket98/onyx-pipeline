"""A hung worker whose grandchild holds the output pipe must not hang the pipeline."""

from __future__ import annotations

import time
from pathlib import Path

from onyx_pipeline.workers import run_capture, run_worker


def test_timeout_kills_grandchildren(tmp_path: Path) -> None:
    # The child backgrounds a grandchild that keeps stdout open, then sleeps itself.
    script = "sleep 300 & sleep 300"
    started = time.monotonic()
    code, out = run_worker(
        ["bash", "-c", script], cwd=tmp_path, timeout=2, log_path=tmp_path / "w.log"
    )
    elapsed = time.monotonic() - started
    assert code == 124
    assert elapsed < 30
    assert "killed after 2s timeout" in (tmp_path / "w.log").read_text()


def test_stdin_is_closed(tmp_path: Path) -> None:
    # A worker that waits on stdin must see EOF immediately, not block.
    code, out, _err = run_capture(
        ["bash", "-c", "read -r x; echo got:$x"], cwd=tmp_path, timeout=10
    )
    assert code in (0, 1)
    assert out.startswith("got:")


def test_short_job_skips_quiet_hours_hold() -> None:
    from datetime import datetime, timedelta

    from onyx_pipeline.jobs import Job
    from onyx_pipeline.notify import ran_long

    now = datetime(2026, 10, 3, 2, 30).astimezone()
    short = Job.__new__(Job)
    short.stages = [{"state": "queued", "at": (now - timedelta(minutes=12)).isoformat()}]
    long = Job.__new__(Job)
    long.stages = [{"state": "queued", "at": (now - timedelta(hours=3)).isoformat()}]
    assert ran_long(short, now) is False
    assert ran_long(long, now) is True
