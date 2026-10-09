"""`onyx-job wait`: the sanctioned way to hand a job's result back to the agent.

The agent runs this as a background exec. When it exits, OpenClaw wakes the agent in
the same session with this output, which keeps inter-session attribution intact.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime

from onyx_pipeline.config import Config
from onyx_pipeline.jobs import Job, job_dir, load_job
from onyx_pipeline.notify import ran_long, seconds_until_quiet_end
from onyx_pipeline.summary import render_summary

# States where the agent has something to tell David.
STOP_STATES = frozenset({"done", "failed", "cancelled", "needs-answer", "needs-approval"})

NEXT_STEP = {
    "done": (
        "Tell David the result as the summary states it. The work is on the job branch, "
        "not merged, so the main checkout will not show it: do not re-check it there or "
        "start another job. Pushing or a PR waits for his yes."
    ),
    "failed": "Tell David what failed. Don't retry unless he asks.",
    "cancelled": "Tell David it was cancelled.",
    "needs-approval": (
        "Paste the plan above to David exactly as written, do not summarise or reword it. "
        "Ask him: approve, or changes? Approve: onyx-job approve <id>, then onyx-job wait "
        '<id> again. Changes: onyx-job revise <id> "<his words>", then onyx-job wait '
        "<id> again. After 3 revisions only approve or cancel remain."
    ),
    "needs-answer": (
        'Ask David the open questions. Then: onyx-job answer <id> "<answer>", '
        "then onyx-job wait <id> again."
    ),
}

NEXT_DELIVERED = {
    "needs-approval": (
        "Next: Reply NO_REPLY. Do not repeat, summarise or comment on the plan. When David "
        'answers: "approve" -> onyx-job approve <id> ; changes -> onyx-job revise <id> '
        '"<his exact words>" ; then run onyx-job wait <id> in the background again.'
    ),
    "needs-answer": (
        "Next: Reply NO_REPLY. Do not repeat, summarise or comment on the questions. When "
        'David answers: onyx-job answer <id> "<his exact words>" ; then run onyx-job wait '
        "<id> in the background again."
    ),
    "done": (
        "Next: Reply NO_REPLY. Do not repeat or comment on it. Pushing or a PR waits for "
        "David's yes."
    ),
    "failed": (
        "Next: Reply NO_REPLY. Do not repeat or comment on it. Pushing or a PR waits for "
        "David's yes."
    ),
    "cancelled": (
        "Next: Reply NO_REPLY. Do not repeat or comment on it. Pushing or a PR waits for "
        "David's yes."
    ),
}

_SETTLE_GRACE_SECONDS = 90.0


def _stop_entered_at(job) -> datetime:  # noqa: ANN001
    if job.stages:
        return datetime.fromisoformat(job.stages[-1]["at"])
    return datetime.now().astimezone()


def _approved_and_waiting(job: Job) -> bool:
    """Approved but the runner has not moved on yet: the plan on screen is stale."""
    return job.state == "needs-approval" and bool(job.flags.get("approved"))


def wait_for_job(
    cfg: Config,
    job_id: str,
    *,
    poll: float = 5.0,
    sleep_fn=time.sleep,  # noqa: ANN001
) -> int:
    try:
        job = load_job(cfg, job_id)
    except FileNotFoundError:
        print(f"error: unknown job {job_id}", file=sys.stderr)
        return 1
    while job.state not in STOP_STATES or _approved_and_waiting(job):
        sleep_fn(poll)
        job = load_job(cfg, job_id)
    while job.flags.get("settled") != job.state:
        entered = _stop_entered_at(job)
        if (datetime.now().astimezone() - entered).total_seconds() >= _SETTLE_GRACE_SECONDS:
            break
        sleep_fn(poll)
        job = load_job(cfg, job_id)
    now = datetime.now().astimezone()
    if job.state == "done" and ran_long(job, now):
        delay = seconds_until_quiet_end(now, cfg.quiet_hours)
        if delay > 0:
            sleep_fn(delay)
    print(f"[onyx-job {job.id} {job.state}]")
    if job.flags.get("delivered") == job.state:
        print("Delivered: the pipeline already sent David the full message.")
        print(NEXT_DELIVERED[job.state].replace("<id>", job.id))
        return 0
    path = job_dir(cfg, job.id) / "summary.md"
    summary = path.read_text() if path.exists() else render_summary(cfg, job)
    print(summary.rstrip())
    if job.state == "needs-approval":
        view_path = job_dir(cfg, job.id) / "plan-view.md"
        if view_path.is_file():
            view = view_path.read_text()
            if view:
                print(view, end="" if view.endswith("\n") else "\n")
    print(f"Next: {NEXT_STEP[job.state].replace('<id>', job.id)}")
    return 0
