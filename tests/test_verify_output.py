"""Verify failures keep a tail and flag commands that never ran the tests."""

from __future__ import annotations

from types import SimpleNamespace

from onyx_pipeline import stages
from onyx_pipeline.jobs import create_job
from onyx_pipeline.prompts import review_prompt
from onyx_pipeline.summary import _verify_text
from tests.helpers import make_git_repo


def test_could_not_run_true_for_127_and_missing_module() -> None:
    assert stages.could_not_run(127, "bash: none: command not found") is True
    assert stages.could_not_run(126, "bash: ./run-tests: Permission denied") is True
    assert stages.could_not_run(1, "/usr/bin/python3: No module named pytest") is True
    assert (
        stages.could_not_run(1, "ls: cannot access 'generated.txt': No such file or directory")
        is False
    )


def test_could_not_run_false_for_long_test_failure() -> None:
    output = "\n".join(
        [
            "FAILED tests/test_x.py::test_a - FileNotFoundError",
            "E   FileNotFoundError: [Errno 2] No such file or directory: '/tmp/x'",
            "E   AssertionError: something broke",
            "1 failed, 1 passed in 0.2s",
        ]
    )
    assert stages.could_not_run(1, output) is False


def test_could_not_run_false_for_plain_failure() -> None:
    assert stages.could_not_run(1, "1 failed, 2 passed in 0.1s") is False
    assert stages.could_not_run(0, "3 passed in 0.1s") is False


def _job_with_verify(onyx_env, monkeypatch, results):
    repo = make_git_repo(onyx_env.roots, "verify-tail")
    job = create_job(onyx_env.cfg, kind="code", repo=str(repo), task="tail")
    monkeypatch.setattr(stages, "_run_in_one_shell", lambda *a, **k: results)
    stages._run_verify(onyx_env.cfg, job, "true")
    return job


def test_run_verify_stores_tail_only_on_failure(onyx_env, monkeypatch) -> None:
    job = _job_with_verify(
        onyx_env,
        monkeypatch,
        [("ok", 0, "everything fine"), ("bad", 1, "boom")],
    )
    assert "tail" not in job.verify_results[0]
    assert job.verify_results[0]["could_not_run"] is False
    assert job.verify_results[1]["tail"] == "boom"
    assert job.verify_results[1]["could_not_run"] is False


def test_run_verify_flags_missing_module(onyx_env, monkeypatch) -> None:
    job = _job_with_verify(
        onyx_env,
        monkeypatch,
        [("python3 -m pytest -q", 1, "/usr/bin/python3: No module named pytest")],
    )
    assert job.verify_results[0]["could_not_run"] is True
    assert job.verify_results[0]["tail"] == "/usr/bin/python3: No module named pytest"


def test_run_verify_caps_tail(onyx_env, monkeypatch) -> None:
    long_output = "\n".join(f"line {i}" for i in range(30))
    job = _job_with_verify(onyx_env, monkeypatch, [("slow", 1, long_output)])
    tail = str(job.verify_results[0]["tail"])
    assert len(tail) <= 1500
    assert len(tail.splitlines()) <= 20
    assert "line 29" in tail


def test_summary_marks_could_not_run() -> None:
    job = SimpleNamespace(
        verify_results=[
            {
                "command": "python3 -m pytest -q",
                "exit_code": 1,
                "tail": "No module named pytest",
                "could_not_run": True,
            },
            {"command": "uv run pytest -q", "exit_code": 0, "could_not_run": False},
        ]
    )
    assert _verify_text(job) == ("python3 -m pytest -q=>1 (could not run), uv run pytest -q=>0")


def test_old_results_without_new_keys_still_render() -> None:
    old = [{"command": "pytest", "exit_code": 1}]
    assert _verify_text(SimpleNamespace(verify_results=old)) == "pytest=>1"
    prompt = review_prompt("the plan", "main", old)
    assert "pytest" in prompt
    assert "exit 1" in prompt
    assert "could not run" not in prompt
