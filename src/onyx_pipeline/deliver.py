"""Send stop-state messages to David via Telegram (no model)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from onyx_pipeline.config import Config
from onyx_pipeline.jobs import Job, job_dir
from onyx_pipeline.notify import in_quiet_hours
from onyx_pipeline.parse import parse_open_questions
from onyx_pipeline.telegram import send_document, send_message, task_title


def _read_summary_lines(cfg: Config, job: Job) -> list[str]:
    path = job_dir(cfg, job.id) / "summary.md"
    if not path.is_file():
        from onyx_pipeline.summary import render_summary

        text = render_summary(cfg, job)
    else:
        text = path.read_text()
    return text.rstrip().splitlines()


REPORT_LIMIT = 14000


def _report_label(kind: str) -> str:
    return "Research" if kind == "research" else "Ops"


def _report_done_message(job: Job, title: str) -> str:
    """Send the report itself: a bare path tells David nothing."""
    path = Path(job.report_path) if job.report_path else None
    body = path.read_text().strip() if path and path.is_file() else ""
    label = _report_label(job.kind)
    if not body:
        where = f" ({path})" if path else ""
        return f"{label} done: {title}\nThe report is empty or missing{where}."
    if len(body) > REPORT_LIMIT:
        body = body[:REPORT_LIMIT].rstrip() + "\n\n[truncated; the rest is in the full report]"
    return f"{label} done: {title}\n\n{body}\n\nFull report: {path}"


def _ops_brief_data(cfg: Config, job: Job) -> dict:
    """The saved brief.json as a dict, or an empty dict when missing or invalid."""
    path = job_dir(cfg, job.id) / "brief.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _ops_summary(cfg: Config, job: Job) -> str:
    summary = _ops_brief_data(cfg, job).get("summary")
    return summary.strip() if isinstance(summary, str) else ""


def _ops_approval_message(cfg: Config, job: Job, title: str) -> str:
    lines = [f"Ops approval needed: {title}"]
    summary = _ops_summary(cfg, job)
    if summary:
        lines.extend(["", summary])
    data = _ops_brief_data(cfg, job)
    steps = data.get("steps")
    state_steps = (
        [s for s in steps if isinstance(s, dict) and s.get("state_change")]
        if isinstance(steps, list)
        else []
    )
    changes = job.flags.get("ops_changes")
    lines.append("")
    if state_steps:
        lines.append("State-changing steps:")
        for step in state_steps:
            lines.append(f"{step.get('number')}. {step.get('action')} — {step.get('exact_values')}")
    if isinstance(changes, list) and changes:
        lines.append("Changes:")
        lines.extend(f"- {change}" for change in changes)
    if not state_steps and not (isinstance(changes, list) and changes):
        lines.append("No state changes listed.")
    lines.extend(["", f"Reply: approve {job.id}  or  no {job.id}"])
    return "\n".join(lines)


def compose_stop_message(cfg: Config, job: Job) -> str:
    title = task_title(job.task)
    directory = job_dir(cfg, job.id)
    state = job.state

    if state == "needs-approval" and job.kind == "ops":
        return _ops_approval_message(cfg, job, title)

    if state == "needs-approval":
        view_path = directory / "plan-view.md"
        view = view_path.read_text() if view_path.is_file() else ""
        revisions = int(job.flags.get("revisions") or 0)
        left = 3 - revisions
        footer = f'Reply "approve" to build it, or tell me what to change (revisions left: {left}).'
        return f"{view.rstrip()}\n\n{footer}" if view else footer

    if state == "needs-answer":
        plan_path = directory / "plan.md"
        questions = parse_open_questions(plan_path.read_text()) if plan_path.is_file() else []
        lines = [f"Claude needs answers before it can plan: {title}"]
        for i, q in enumerate(questions, 1):
            lines.append(f"{i}. {q}")
        lines.append("Reply with your answers.")
        return "\n".join(lines)

    if state == "done" and job.kind in {"research", "ops"}:
        return _report_done_message(job, title)

    if state == "done":
        lines = [f"Job done: {title}"]
        lines.extend(_read_summary_lines(cfg, job))
        auto_quote = job.flags.get("auto_quote")
        if auto_quote:
            lines.append(f'Plan check skipped because you said: "{auto_quote}"')
        lines.append("Nothing is pushed. Tell me if you want a push or a PR.")
        return "\n".join(lines)

    if state == "failed":
        lines = [f"Job failed: {title}"]
        lines.extend(_read_summary_lines(cfg, job))
        lines.append("Tell me if you want it retried.")
        return "\n".join(lines)

    if state == "cancelled":
        return f"Job cancelled: {title}"

    return ""


def _send_report_document(cfg: Config, job: Job, silent: bool) -> bool:
    """Send a finished report as a file; False means fall back to text."""
    if job.state != "done" or job.kind not in {"research", "ops"} or not job.report_path:
        return False
    path = Path(job.report_path)
    if not path.is_file() or not path.read_text().strip():
        return False
    caption = f"{_report_label(job.kind)} done: {task_title(job.task)}"
    try:
        sent = send_document(cfg, path, caption, silent=silent)
    except OSError:
        return False
    return sent is not None


def deliver_stop(cfg: Config, job: Job) -> bool:
    if cfg.no_deliver:
        return False
    now = datetime.now().astimezone()
    silent = in_quiet_hours(now, cfg.quiet_hours)
    if _send_report_document(cfg, job, silent):
        return True
    text = compose_stop_message(cfg, job)
    if not text:
        return False
    try:
        last_id = send_message(cfg, text, silent=silent)
    except Exception:  # noqa: BLE001
        return False
    return last_id is not None
