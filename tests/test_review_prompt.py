"""The reviewer must see the verify results and must not rerun tests itself."""

from __future__ import annotations

from onyx_pipeline.jobs import create_job, load_job
from onyx_pipeline.prompts import review_prompt
from onyx_pipeline.stages import run_loop
from tests.helpers import make_git_repo

NO_SANDBOX_SENTENCE = (
    "These commands were already run outside your sandbox with the results above. "
    "Do not run tests yourself and do not fail the review because you cannot run them. "
    "Judge the code and the test coverage."
)


def test_review_prompt_lists_verify_commands_and_exit_codes() -> None:
    prompt = review_prompt(
        "the plan",
        "main",
        [
            {"command": "uv run pytest -q", "exit_code": 0, "tail": ""},
            {"command": "uv run ruff check", "exit_code": 1, "tail": "lint error"},
        ],
    )
    assert "uv run pytest -q" in prompt
    assert "uv run ruff check" in prompt
    assert "exit 0" in prompt
    assert "exit 1" in prompt
    assert NO_SANDBOX_SENTENCE in prompt
    assert "no verify commands ran" not in prompt


def test_review_prompt_empty_results_says_none_ran() -> None:
    for empty in ([], None):
        prompt = review_prompt("the plan", "main", empty)
        assert "no verify commands ran" in prompt
        assert NO_SANDBOX_SENTENCE in prompt


def test_review_prompt_skipped_verify_says_none_ran() -> None:
    prompt = review_prompt(
        "the plan",
        "main",
        [{"command": None, "exit_code": None, "skipped": True}],
    )
    assert "no verify commands ran" in prompt
    assert NO_SANDBOX_SENTENCE in prompt


def test_review_prompt_keeps_verdict_instruction() -> None:
    prompt = review_prompt("the plan", "main", [{"command": "true", "exit_code": 0}])
    assert "VERDICT: PASS or VERDICT: FAIL." in prompt
    assert "git diff main...HEAD" in prompt


def test_review_prompt_shows_failure_tail_and_could_not_run() -> None:
    prompt = review_prompt(
        "the plan",
        "main",
        [
            {
                "command": "python3 -m pytest -q",
                "exit_code": 1,
                "tail": "/usr/bin/python3: No module named pytest",
                "could_not_run": True,
            },
            {"command": "uv run ruff check", "exit_code": 1, "tail": "E501 line too long"},
        ],
    )
    # the tail is indented under its command's line
    assert "    /usr/bin/python3: No module named pytest" in prompt
    assert "    E501 line too long" in prompt
    # the could-not-run wording sits on the command's line
    assert (
        "- `python3 -m pytest -q` — exit 1 — "
        "could not run (environment problem, not a test failure)" in prompt
    )


def test_pipeline_passes_verify_results_to_reviewer(onyx_env, monkeypatch) -> None:
    monkeypatch.setenv("STUB_CLAUDE_MODE", "ok")
    monkeypatch.setenv("STUB_REVIEW", "pass")
    monkeypatch.setenv("STUB_VERIFY_CMD", "true")
    repo = make_git_repo(onyx_env.roots, "review-verify")
    job = create_job(
        onyx_env.cfg, kind="code", repo=str(repo), task="Show verify results to review"
    )
    run_loop(onyx_env.cfg)
    job = load_job(onyx_env.cfg, job.id)
    assert job.state == "done"
    log = (onyx_env.jobs / ".worker-log").read_text()
    assert "Verify results (already run outside your sandbox):" in log
    assert "- `true` — exit 0" in log
    assert "no verify commands ran" not in log
