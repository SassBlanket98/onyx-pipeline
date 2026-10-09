"""Ops flow: briefing, approval gate, operation, verification, delivery.

The Claude/Chrome worker functions do not exist yet (slice 4 replaces the stubs),
so every worker call is stubbed here. No real agent, telegram or network runs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from onyx_pipeline import stages
from onyx_pipeline.config import load_config
from onyx_pipeline.deliver import compose_stop_message
from onyx_pipeline.jobs import create_job, is_runnable, job_dir, load_job
from onyx_pipeline.ops_brief import render_operator_brief
from onyx_pipeline.stages import approve_job, cancel_job, process_job

BRIEF = {
    "version": 1,
    "summary": "Point the service at the new host.",
    "steps": [
        {
            "number": 1,
            "action": "Read the current record for example.com",
            "state_change": False,
            "exact_values": "example.com",
            "verification": "dig example.com +short",
        },
        {
            "number": 2,
            "action": "Set the A record to 203.0.113.10",
            "state_change": True,
            "exact_values": "A=203.0.113.10",
            "verification": "dig example.com +short",
        },
    ],
    "changes": ["2: set A=203.0.113.10 for example.com"],
}


def _brief_text(body: str = "# Operator brief\nPoint the service at the new host.\n") -> str:
    return f"{body}\n```json\n{json.dumps(BRIEF)}\n```\n"


@pytest.fixture
def ops_env(onyx_env, monkeypatch):
    """Delivery enabled and fully offline: no real telegram, no notifications."""
    monkeypatch.delenv("ONYX_NO_NOTIFY", raising=False)
    monkeypatch.delenv("ONYX_NO_DELIVER", raising=False)
    monkeypatch.setenv("ONYX_OPENCLAW_CONFIG", str(onyx_env.tmp / "no-openclaw.json"))
    monkeypatch.setattr(stages, "notify_job", lambda cfg, job: None)
    return load_config()


def _stub_brief(monkeypatch, text: str | None = None):
    reply = _brief_text() if text is None else text
    monkeypatch.setattr(stages, "brief_ops", lambda cfg, directory, task: (0, reply))


def _stub_send(monkeypatch, message_id: int | None = 555, sink: list[str] | None = None):
    def fake(cfg, text, *, silent):
        if sink is not None:
            sink.append(text)
        return message_id

    monkeypatch.setattr(stages, "send_message", fake)


def _stub_workers(monkeypatch, operate_calls: list, result: str = "RESULT: confirmed\n"):
    def fake_operate(cfg, directory, brief_md, timeout=None):
        operate_calls.append(brief_md)
        return (0, "operated")

    monkeypatch.setattr(stages, "operate_ops", fake_operate)
    monkeypatch.setattr(
        stages, "verify_ops", lambda cfg, directory, brief, timeout=None: (0, result)
    )


def test_brief_stops_for_approval_and_never_operates(ops_env, monkeypatch):
    cfg = ops_env
    sent: list[str] = []
    operates: list[str] = []
    _stub_brief(monkeypatch)
    _stub_send(monkeypatch, 555, sent)
    _stub_workers(monkeypatch, operates)

    job = create_job(cfg, kind="ops", task="Point example.com at the new host", repo=None)
    process_job(cfg, job)

    stored = load_job(cfg, job.id)
    assert cfg.auto_approve is True  # ops ignores it: it always stops for approval
    assert stored.state == "needs-approval"
    assert operates == []
    assert stored.flags["ops_approval_message_id"] == 555
    assert stored.flags["ops_changes"] == BRIEF["changes"]
    assert stored.flags["ops_brief_sha256"]
    assert is_runnable(stored) is False

    assert len(sent) == 1
    text = sent[0]
    assert BRIEF["summary"] in text
    for change in BRIEF["changes"]:
        assert change in text
    assert f"Reply: approve {job.id}  or  no {job.id}" in text

    assert (job_dir(cfg, job.id) / "brief.json").is_file()
    brief_md = (job_dir(cfg, job.id) / "brief.md").read_text()
    assert "# Operator brief" in brief_md
    assert "```json" not in brief_md


def test_rerun_while_waiting_does_not_rebrief_or_send(ops_env, monkeypatch):
    cfg = ops_env
    sent: list[str] = []
    _stub_brief(monkeypatch)
    _stub_send(monkeypatch, 555, sent)
    _stub_workers(monkeypatch, [])

    job = create_job(cfg, kind="ops", task="Restart the web server", repo=None)
    process_job(cfg, job)
    process_job(cfg, load_job(cfg, job.id))

    assert len(sent) == 1
    assert load_job(cfg, job.id).state == "needs-approval"


def test_delivery_failure_leaves_waiting_and_unapprovable(ops_env, monkeypatch):
    cfg = ops_env
    sent: list[str] = []
    now = [1000.0]
    monkeypatch.setattr("time.time", lambda: now[0])
    _stub_brief(monkeypatch)
    _stub_send(monkeypatch, None, sent)
    _stub_workers(monkeypatch, [])

    job = create_job(cfg, kind="ops", task="Renew the certificate", repo=None)
    process_job(cfg, job)

    stored = load_job(cfg, job.id)
    assert stored.state == "needs-approval"
    assert stored.flags["ops_approval_message_id"] is None
    assert is_runnable(stored) is False  # backoff: the runner retries it later
    now[0] += 60
    assert is_runnable(stored) is True
    with pytest.raises(ValueError, match="never delivered"):
        approve_job(cfg, job.id)


def test_approve_rejects_a_changed_brief(ops_env, monkeypatch):
    cfg = ops_env
    _stub_brief(monkeypatch)
    _stub_send(monkeypatch, 555)
    _stub_workers(monkeypatch, [])

    job = create_job(cfg, kind="ops", task="Point example.com", repo=None)
    process_job(cfg, job)
    changed = dict(BRIEF, summary="A different plan entirely.")
    (job_dir(cfg, job.id) / "brief.json").write_text(json.dumps(changed, indent=2) + "\n")

    with pytest.raises(ValueError, match="changed"):
        approve_job(cfg, job.id)
    assert load_job(cfg, job.id).flags["approved"] is False


def test_changed_brief_after_approval_fails_without_operating(ops_env, monkeypatch):
    cfg = ops_env
    operates: list[str] = []
    _stub_brief(monkeypatch)
    _stub_send(monkeypatch, 555)
    _stub_workers(monkeypatch, operates)

    job = create_job(cfg, kind="ops", task="Point example.com", repo=None)
    process_job(cfg, job)
    approve_job(cfg, job.id)
    changed = dict(BRIEF, summary="A different plan entirely.")
    (job_dir(cfg, job.id) / "brief.json").write_text(json.dumps(changed, indent=2) + "\n")

    process_job(cfg, load_job(cfg, job.id))

    stored = load_job(cfg, job.id)
    assert stored.state == "failed"
    assert operates == []
    assert "changed" in (stored.error or "")


def test_no_cancel_never_operates(ops_env, monkeypatch):
    cfg = ops_env
    operates: list[str] = []
    _stub_brief(monkeypatch)
    _stub_send(monkeypatch, 555)
    _stub_workers(monkeypatch, operates)

    job = create_job(cfg, kind="ops", task="Restart nginx", repo=None)
    process_job(cfg, job)
    cancel_job(cfg, job.id)

    stored = load_job(cfg, job.id)
    assert stored.state == "cancelled"
    assert operates == []
    assert is_runnable(stored) is False
    with pytest.raises(ValueError):
        approve_job(cfg, job.id)


def test_happy_path_reaches_done_with_report(ops_env, monkeypatch):
    cfg = ops_env
    operates: list[str] = []
    _stub_brief(monkeypatch)
    _stub_send(monkeypatch, 555)
    _stub_workers(monkeypatch, operates, "RESULT: confirmed\nThe record now points at it.\n")

    job = create_job(cfg, kind="ops", task="Point example.com at the new host", repo=None)
    process_job(cfg, job)
    assert load_job(cfg, job.id).state == "needs-approval"

    approve_job(cfg, job.id)
    assert is_runnable(load_job(cfg, job.id)) is True
    process_job(cfg, load_job(cfg, job.id))

    stored = load_job(cfg, job.id)
    assert stored.state == "done"
    assert stored.report_path
    assert Path(stored.report_path).is_file()
    report = (job_dir(cfg, job.id) / "report.md").read_text()
    assert report.startswith("RESULT: confirmed")
    assert len(operates) == 1
    assert operates[0] == render_operator_brief(BRIEF)

    message = compose_stop_message(cfg, stored)
    assert message.startswith("Ops done:")
    assert "The record now points at it." in message


def test_uncertain_verification_fails_with_report(ops_env, monkeypatch):
    cfg = ops_env
    operates: list[str] = []
    _stub_brief(monkeypatch)
    _stub_send(monkeypatch, 555)
    _stub_workers(monkeypatch, operates, "RESULT: uncertain\nThe change may not have applied.\n")

    job = create_job(cfg, kind="ops", task="Point example.com", repo=None)
    process_job(cfg, job)
    approve_job(cfg, job.id)
    process_job(cfg, load_job(cfg, job.id))

    stored = load_job(cfg, job.id)
    assert stored.state == "failed"
    assert "uncertain" in (stored.error or "")
    assert stored.report_path
    report = (job_dir(cfg, job.id) / "report.md").read_text()
    assert report.startswith("RESULT: uncertain")


def test_malformed_brief_fails_with_a_reason(ops_env, monkeypatch):
    cfg = ops_env
    sent: list[str] = []
    _stub_send(monkeypatch, 555, sent)
    _stub_brief(monkeypatch, "# Brief with no structured part\n")
    _stub_workers(monkeypatch, [])

    job = create_job(cfg, kind="ops", task="Point example.com", repo=None)
    process_job(cfg, job)

    stored = load_job(cfg, job.id)
    assert stored.state == "failed"
    assert "invalid" in (stored.error or "")
    assert sent == []
    assert stored.flags["ops_approval_message_id"] is None


def test_changed_brief_md_after_approval_fails_without_operating(ops_env, monkeypatch):
    """Item 1: the digest covers brief.md too, not just brief.json."""
    cfg = ops_env
    operates: list[str] = []
    _stub_brief(monkeypatch)
    _stub_send(monkeypatch, 555)
    _stub_workers(monkeypatch, operates)

    job = create_job(cfg, kind="ops", task="Point example.com", repo=None)
    process_job(cfg, job)
    approve_job(cfg, job.id)
    (job_dir(cfg, job.id) / "brief.md").write_text("# Operator brief\nA different body.\n")

    process_job(cfg, load_job(cfg, job.id))

    stored = load_job(cfg, job.id)
    assert stored.state == "failed"
    assert operates == []
    assert "changed" in (stored.error or "")


def test_operator_failure_records_report_and_skips_verify(ops_env, monkeypatch):
    """Item 2: a nonzero operate exit becomes a report and never verifies."""
    cfg = ops_env
    operates: list[str] = []
    verifies: list[object] = []
    _stub_brief(monkeypatch)
    _stub_send(monkeypatch, 555)

    def fake_operate(cfg, directory, brief_md, timeout=None):
        operates.append(brief_md)
        return (2, "chrome could not reach the site")

    def fake_verify(cfg, directory, brief, timeout=None):
        verifies.append(brief)
        return (0, "RESULT: confirmed\n")

    monkeypatch.setattr(stages, "operate_ops", fake_operate)
    monkeypatch.setattr(stages, "verify_ops", fake_verify)

    job = create_job(cfg, kind="ops", task="Point example.com", repo=None)
    process_job(cfg, job)
    approve_job(cfg, job.id)
    process_job(cfg, load_job(cfg, job.id))

    stored = load_job(cfg, job.id)
    assert stored.state == "failed"
    assert "exit 2" in (stored.error or "")
    report = (job_dir(cfg, job.id) / "report.md").read_text()
    assert report.startswith("Operator failed (exit 2)")
    assert "chrome could not reach the site" in report
    assert verifies == []
    assert len(operates) == 1


def test_approval_message_lists_every_state_change_step(ops_env, monkeypatch):
    """Item 3: only state_change steps, in number/action/values form, before changes."""
    cfg = ops_env
    sent: list[str] = []
    _stub_brief(monkeypatch)
    _stub_send(monkeypatch, 555, sent)
    _stub_workers(monkeypatch, [])

    job = create_job(cfg, kind="ops", task="Point example.com", repo=None)
    process_job(cfg, job)

    text = sent[0]
    assert "2. Set the A record to 203.0.113.10 — A=203.0.113.10" in text
    assert "1. Read the current record for example.com" not in text
    for change in BRIEF["changes"]:
        assert change in text
    assert text.index("2. Set the A record") < text.index("Changes:")


def test_failed_delivery_is_retried_on_next_pass(ops_env, monkeypatch):
    """Item 4: no message id makes the job runnable once the retry delay has passed."""
    cfg = ops_env
    sent: list[str] = []
    replies = iter([None, 555])
    now = [1000.0]
    monkeypatch.setattr("time.time", lambda: now[0])

    def fake_send(cfg, text, *, silent):
        sent.append(text)
        return next(replies)

    monkeypatch.setattr(stages, "send_message", fake_send)
    _stub_brief(monkeypatch)

    job = create_job(cfg, kind="ops", task="Renew the certificate", repo=None)
    process_job(cfg, job)

    first = load_job(cfg, job.id)
    assert first.flags["ops_approval_message_id"] is None
    assert is_runnable(first) is False  # backoff: the retry is not due yet

    now[0] += 60
    assert is_runnable(first) is True

    process_job(cfg, load_job(cfg, job.id))

    stored = load_job(cfg, job.id)
    assert stored.flags["ops_approval_message_id"] == 555
    assert is_runnable(stored) is False
    assert len(sent) == 2


def test_operator_runs_from_the_approved_steps_not_brief_md(ops_env, monkeypatch):
    """Item 1: the operator prompt is the rendered structured brief, not brief.md."""
    cfg = ops_env
    operates: list[str] = []
    _stub_brief(monkeypatch)
    _stub_send(monkeypatch, 555)
    _stub_workers(monkeypatch, operates)

    job = create_job(cfg, kind="ops", task="Point example.com at the new host", repo=None)
    process_job(cfg, job)
    approve_job(cfg, job.id)
    process_job(cfg, load_job(cfg, job.id))

    assert len(operates) == 1
    prompt = operates[0]
    assert BRIEF["summary"] in prompt
    for step in BRIEF["steps"]:
        assert f"{step['number']}. {step['action']}" in prompt
        assert step["exact_values"] in prompt
        assert step["verification"] in prompt
    assert prompt != (job_dir(cfg, job.id) / "brief.md").read_text()


def test_verify_confirmed_needs_exit_zero(ops_env, monkeypatch):
    """Item 2: a confirmed first line is not enough when the verifier exits nonzero."""
    cfg = ops_env
    _stub_brief(monkeypatch)
    _stub_send(monkeypatch, 555)
    _stub_workers(monkeypatch, [], "RESULT: confirmed\n")
    monkeypatch.setattr(
        stages,
        "verify_ops",
        lambda cfg, directory, brief, timeout=None: (1, "RESULT: confirmed\n"),
    )

    job = create_job(cfg, kind="ops", task="Point example.com", repo=None)
    process_job(cfg, job)
    approve_job(cfg, job.id)
    process_job(cfg, load_job(cfg, job.id))

    stored = load_job(cfg, job.id)
    assert stored.state == "failed"
    assert "verify exited 1" in (stored.error or "")


def test_operator_failure_sets_report_path_and_delivers(ops_env, monkeypatch):
    """Item 3: the failure report is recorded and delivered like a finished report."""
    cfg = ops_env
    sent: list[str] = []
    _stub_brief(monkeypatch)
    _stub_send(monkeypatch, 555)

    def fake_send(cfg, text, *, silent):
        sent.append(text)
        return 555

    # _stop delivers through deliver.py, which holds its own send_message reference.
    monkeypatch.setattr("onyx_pipeline.deliver.send_message", fake_send)
    monkeypatch.setattr(
        stages,
        "operate_ops",
        lambda cfg, directory, brief_md, timeout=None: (2, "chrome could not reach the site"),
    )

    job = create_job(cfg, kind="ops", task="Point example.com", repo=None)
    process_job(cfg, job)
    approve_job(cfg, job.id)
    process_job(cfg, load_job(cfg, job.id))

    stored = load_job(cfg, job.id)
    assert stored.state == "failed"
    assert stored.report_path
    report_path = Path(stored.report_path)
    assert report_path.is_file()
    assert report_path.read_text().startswith("Operator failed (exit 2)")
    assert stored.flags.get("delivered") == "failed"
    assert sent and sent[-1].startswith("Job failed: Point example.com")


def test_failed_delivery_backs_off_then_fails_the_job(ops_env, monkeypatch):
    """Item 1: retries after 60/120/240 s, then fails after the fourth attempt."""
    cfg = ops_env
    sent: list[str] = []
    now = [1000.0]
    monkeypatch.setattr("time.time", lambda: now[0])

    def fake_send(cfg, text, *, silent):
        sent.append(text)
        return None

    monkeypatch.setattr(stages, "send_message", fake_send)
    _stub_brief(monkeypatch)
    _stub_workers(monkeypatch, [])

    job = create_job(cfg, kind="ops", task="Renew the certificate", repo=None)
    process_job(cfg, job)

    first = load_job(cfg, job.id)
    assert first.state == "needs-approval"
    assert first.flags["ops_delivery_attempts"] == 1
    assert first.flags["ops_next_delivery_at"] == 1060.0
    assert is_runnable(first) is False

    expected_backoff = [120.0, 240.0]
    for offset in expected_backoff:
        now[0] = float(first.flags["ops_next_delivery_at"])  # the retry is due
        assert is_runnable(load_job(cfg, job.id)) is True
        process_job(cfg, load_job(cfg, job.id))
        first = load_job(cfg, job.id)
        assert first.flags["ops_next_delivery_at"] == now[0] + offset

    now[0] = float(first.flags["ops_next_delivery_at"])
    assert is_runnable(load_job(cfg, job.id)) is True
    process_job(cfg, load_job(cfg, job.id))

    stored = load_job(cfg, job.id)
    assert stored.state == "failed"
    assert stored.error == "could not deliver the approval request"
    assert stored.flags["ops_delivery_attempts"] == 4
    assert is_runnable(stored) is False
    assert len(sent) == 4


def test_revise_ops_requeues_and_clears_brief(ops_env, monkeypatch):
    """Item 2: revising an ops job re-briefs from task.md instead of planning."""
    cfg = ops_env
    tasks: list[str] = []
    sent: list[str] = []

    def fake_brief(cfg, directory, task):
        tasks.append(task)
        return (0, _brief_text())

    monkeypatch.setattr(stages, "brief_ops", fake_brief)
    _stub_send(monkeypatch, 555, sent)
    _stub_workers(monkeypatch, [])

    job = create_job(cfg, kind="ops", task="Point example.com at the new host", repo=None)
    process_job(cfg, job)
    assert load_job(cfg, job.id).state == "needs-approval"

    feedback = "Use 203.0.113.20 instead of .10"
    revised = stages.revise_job(cfg, job.id, feedback)

    assert revised.state == "queued"
    assert revised.flags["approved"] is False
    assert revised.flags["revisions"] == 1
    assert revised.flags["ops_approval_message_id"] is None
    assert revised.flags["ops_brief_sha256"] is None
    assert revised.flags["ops_changes"] is None
    assert not (job_dir(cfg, job.id) / "brief.md").exists()
    assert not (job_dir(cfg, job.id) / "brief.json").exists()
    task_md = (job_dir(cfg, job.id) / "task.md").read_text()
    assert "## Plan feedback (round 1)" in task_md
    assert feedback in task_md

    process_job(cfg, load_job(cfg, job.id))

    stored = load_job(cfg, job.id)
    assert stored.state == "needs-approval"
    assert feedback in tasks[-1]  # the re-brief saw the feedback
    assert tasks[-1] == (job_dir(cfg, job.id) / "task.md").read_text()
    assert len(sent) == 2
