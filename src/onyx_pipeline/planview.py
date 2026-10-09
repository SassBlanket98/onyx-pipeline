"""Plain-text rendering of a plan for David's approval."""

from __future__ import annotations

from pathlib import Path

from onyx_pipeline.jobs import Job
from onyx_pipeline.parse import parse_plan_parts, parse_plan_trailers, parse_section
from onyx_pipeline.workers import COMPOSER_MODEL, GROK_MODEL, builder_label

TASK_LIMIT = 1200
SECTION_LIMIT = 1500
FEEDBACK_LIMIT = 3000
FEEDBACK_MARK = "## Plan feedback"


def _trim(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _section(plan_text: str, heading: str) -> str:
    body = parse_section(plan_text, heading)
    return _trim(body, SECTION_LIMIT) if body else "(none given)"


def _task_block(task_text: str) -> str:
    """Trim the original task, but always keep the revision feedback (latest rounds win)."""
    head, mark, feedback = task_text.partition(FEEDBACK_MARK)
    if not mark:
        return _trim(task_text, TASK_LIMIT)
    feedback = mark + feedback
    if len(feedback) > FEEDBACK_LIMIT:
        feedback = "…" + feedback[-FEEDBACK_LIMIT:]
    return _trim(head.rstrip(), TASK_LIMIT) + "\n" + feedback.rstrip()


def _builder_line(plan_text: str, judgment: str, max_parts: int, cheap_model: str) -> str:
    parts, _note = parse_plan_parts(plan_text, max_parts=max_parts)
    if len(parts) >= 2:
        each = ", ".join(
            f"part {p.n} {builder_label(GROK_MODEL if p.judgment == 'needed' else cheap_model)}"
            for p in parts
        )
        return f"BUILDERS: {each}"
    return f"BUILDER: {builder_label(GROK_MODEL if judgment == 'needed' else cheap_model)}"


def _investigator_lines(job: Job) -> list[str]:
    triage = job.flags.get("triage")
    if not isinstance(triage, dict):
        return []
    who = "Grok 4.6 high" if triage.get("deep") else "Composer"
    source = str(triage.get("source") or "")
    reasons = ", ".join(str(r) for r in triage.get("reasons") or [])
    why = f"you asked ({source})" if source.startswith("--") else reasons or "nothing risky found"
    return [f"Investigated by: {who} ({why})"]


def render_plan_view(
    job: Job,
    task_text: str,
    plan_text: str,
    plan_path: Path,
    *,
    max_parts: int = 3,
    cheap_model: str = COMPOSER_MODEL,
) -> str:
    revisions = int(job.flags.get("revisions") or 0)
    title = "PLAN FOR APPROVAL"
    if revisions:
        title = f"{title}  (revision {revisions} of 3)"
    size, judgment, verify = parse_plan_trailers(plan_text)
    builder = _builder_line(plan_text, judgment, max_parts, cheap_model)
    checked = _trim(verify, SECTION_LIMIT) if verify else "no verify command"
    lines = [
        title,
        f"Job: {job.id}",
        *_investigator_lines(job),
        "",
        "TASK AS SENT TO CLAUDE",
        _task_block(task_text),
        "",
        "GOAL",
        _section(plan_text, "GOAL"),
        "",
        "WHAT WILL CHANGE",
        _section(plan_text, "CHANGES"),
        "",
        "OUT OF SCOPE",
        _section(plan_text, "OUT OF SCOPE"),
        "",
        "ASSUMPTIONS AND DEVIATIONS",
        _section(plan_text, "ASSUMPTIONS AND DEVIATIONS"),
        "",
        "HOW IT WILL BE CHECKED",
        checked,
        "",
        f"SIZE: {size}   {builder}",
        f"Full plan: {plan_path}",
        "",
    ]
    return "\n".join(lines)
