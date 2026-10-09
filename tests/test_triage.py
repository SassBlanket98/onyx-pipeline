"""Investigator triage: which tasks go to Grok and which stay on Composer."""

from __future__ import annotations

import pytest

from onyx_pipeline.triage import triage_task

GROK = [
    "Audit the application's security and update the README",
    "Add authentication to the admin routes",
    "Check that the user is authorized before deleting a record",
    "Add two separate demo accounts to the Hydrol dashboard, full access, test all three logins",
    "Add a password reset flow to the login page",
    "Add Stripe billing for the reseller subscription",
    "Back up the production Postgres database nightly and delete backups older than 7 days",
    "Expose the dashboard on a public URL through the Tailscale funnel with systemd restart",
    "Figure out why the bot sometimes drops messages",
    "Refactor every route in the web app to use the new data layer",
]
COMPOSER = [
    "Add an author bio to the blog post",
    "Show the authors list on the about page",
    "Add a one-line docstring to the cli main function",
    "Fix the typo in the README",
    "Add a bench models subcommand that lists every model",
    "Rename the helper function parse_args to read_args",
    "Add a k flag to the CLI that limits results",
]


@pytest.mark.parametrize("task", GROK)
def test_risky_tasks_go_to_grok(task: str) -> None:
    result = triage_task(task)
    assert result.deep, (task, result)
    assert result.reasons


@pytest.mark.parametrize("task", COMPOSER)
def test_plain_tasks_stay_on_composer(task: str) -> None:
    result = triage_task(task)
    assert not result.deep, (task, result)


def test_small_task_cancels_a_little_risk() -> None:
    assert not triage_task("Fix the typo in the login page heading").deep


def test_long_multi_part_task_adds_up() -> None:
    task = "Update the report. " + " ".join(f"{i}. Change the {i}th table." for i in range(1, 6))
    task += " " + "Keep the layout. " * 40
    assert triage_task(task, threshold=2).deep
    assert not triage_task(task, threshold=10).deep
