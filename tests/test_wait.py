from __future__ import annotations

from onyx_pipeline.jobs import create_job, load_job, save_job
from onyx_pipeline.wait import wait_for_job


def test_wait_returns_when_job_finishes(onyx_env, capsys) -> None:
    job = create_job(onyx_env.cfg, kind="research", task="Look something up", repo=None)
    polls = []

    def fake_sleep(seconds: float) -> None:
        polls.append(seconds)
        if len(polls) == 2:
            current = load_job(onyx_env.cfg, job.id)
            current.state = "done"
            current.flags["settled"] = "done"
            save_job(onyx_env.cfg, current)

    assert wait_for_job(onyx_env.cfg, job.id, poll=0.01, sleep_fn=fake_sleep) == 0
    out = capsys.readouterr().out
    assert f"[onyx-job {job.id} done]" in out
    assert "Next: Tell David the result as the summary states it." in out
    assert len(polls) == 2


def test_wait_stops_at_approval_gate(onyx_env, capsys) -> None:
    job = create_job(onyx_env.cfg, kind="research", task="Big thing", repo=None)
    job.state = "needs-approval"
    job.flags["settled"] = "needs-approval"
    save_job(onyx_env.cfg, job)
    assert wait_for_job(onyx_env.cfg, job.id, poll=0.01, sleep_fn=lambda s: None) == 0
    assert f"onyx-job approve {job.id}" in capsys.readouterr().out


def test_wait_delivered_skips_summary(onyx_env, capsys) -> None:
    job = create_job(onyx_env.cfg, kind="research", task="Delivered task", repo=None)
    job.state = "done"
    job.flags["delivered"] = "done"
    job.flags["settled"] = "done"
    save_job(onyx_env.cfg, job)
    assert wait_for_job(onyx_env.cfg, job.id, poll=0.01, sleep_fn=lambda s: None) == 0
    out = capsys.readouterr().out
    assert "Delivered: the pipeline already sent David the full message." in out
    assert "Reply NO_REPLY" in out
    assert "Tell David the result" not in out


def test_wait_after_approve_does_not_return_stale_plan(onyx_env, capsys) -> None:
    job = create_job(onyx_env.cfg, kind="research", task="Approved but not yet advanced", repo=None)
    current = load_job(onyx_env.cfg, job.id)
    current.state = "needs-approval"
    current.flags["approved"] = True
    current.flags["settled"] = "needs-approval"
    save_job(onyx_env.cfg, current)
    polls = []

    def fake_sleep(seconds: float) -> None:
        polls.append(seconds)
        if len(polls) == 2:
            later = load_job(onyx_env.cfg, job.id)
            later.state = "done"
            later.flags["settled"] = "done"
            save_job(onyx_env.cfg, later)

    assert wait_for_job(onyx_env.cfg, job.id, poll=0.01, sleep_fn=fake_sleep) == 0
    assert len(polls) == 2
    assert f"[onyx-job {job.id} done]" in capsys.readouterr().out


def test_wait_delivered_approval_names_job_id(onyx_env, capsys) -> None:
    job = create_job(onyx_env.cfg, kind="research", task="Plan delivered", repo=None)
    job.state = "needs-approval"
    job.flags["delivered"] = "needs-approval"
    job.flags["settled"] = "needs-approval"
    save_job(onyx_env.cfg, job)
    assert wait_for_job(onyx_env.cfg, job.id, poll=0.01, sleep_fn=lambda s: None) == 0
    out = capsys.readouterr().out
    assert f"onyx-job approve {job.id}" in out
    assert f"onyx-job revise {job.id}" in out
    assert f"onyx-job wait {job.id}" in out
    assert "No job id is needed" not in out


def test_wait_delivered_answer_names_job_id(onyx_env, capsys) -> None:
    job = create_job(onyx_env.cfg, kind="research", task="Questions delivered", repo=None)
    job.state = "needs-answer"
    job.flags["delivered"] = "needs-answer"
    job.flags["settled"] = "needs-answer"
    save_job(onyx_env.cfg, job)
    assert wait_for_job(onyx_env.cfg, job.id, poll=0.01, sleep_fn=lambda s: None) == 0
    out = capsys.readouterr().out
    assert f"onyx-job answer {job.id}" in out
    assert f"onyx-job wait {job.id}" in out
    assert "No job id is needed" not in out


def test_instructions_always_carry_the_job_id() -> None:
    from onyx_pipeline.wait import NEXT_DELIVERED, NEXT_STEP

    for text in [*NEXT_DELIVERED.values(), *NEXT_STEP.values()]:
        assert "No job id is needed" not in text
    for state in ("needs-approval", "needs-answer"):
        assert "<id>" in NEXT_DELIVERED[state]
        assert "<id>" in NEXT_STEP[state]
