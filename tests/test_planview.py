"""Unit tests for the plan-approval rendering."""

from __future__ import annotations

from pathlib import Path

from onyx_pipeline.jobs import Job
from onyx_pipeline.planview import render_plan_view

PLAN_PATH = Path("/tmp/jobs/sample/plan.md")

FULL_PLAN = """\
# Plan
intro

## GOAL
Ship the marker file.

## CHANGES
- a.py — add helper
- b.py — call it

## OUT OF SCOPE
- docs

## ASSUMPTIONS AND DEVIATIONS
none

## PARTS
PART 1: A | FILES: a.py | AFTER: none | JUDGMENT: none
steps

SIZE: small
JUDGMENT: none
VERIFY: pytest -q
"""

TASK = """\
# Task

Add a helper.

## Answers
Use JSON.

## Plan feedback (round 1)
Keep it small.
"""


def _job(**overrides: object) -> Job:
    flags = {"approved": False, "answer": None, "auto": False, "revisions": 0}
    extra_flags = overrides.pop("flags", None)
    if isinstance(extra_flags, dict):
        flags.update(extra_flags)
    return Job(
        id="20260101-120000-sample",
        kind="code",
        repo="/tmp/repo",
        task="Add a helper",
        state="needs-approval",
        stages=[],
        flags=flags,
        **overrides,  # type: ignore[arg-type]
    )


def test_render_includes_all_sections_and_builder_none() -> None:
    text = render_plan_view(_job(), TASK, FULL_PLAN, PLAN_PATH)
    assert text.startswith("PLAN FOR APPROVAL\n")
    assert "(revision" not in text
    assert "Job: 20260101-120000-sample" in text
    assert "TASK AS SENT TO CLAUDE" in text
    assert "## Answers" in text
    assert "## Plan feedback (round 1)" in text
    assert "GOAL\nShip the marker file." in text
    assert "WHAT WILL CHANGE\n- a.py — add helper\n- b.py — call it" in text
    assert "OUT OF SCOPE\n- docs" in text
    assert "ASSUMPTIONS AND DEVIATIONS\nnone" in text
    assert "HOW IT WILL BE CHECKED\npytest -q" in text
    assert "SIZE: small   BUILDER: Composer 2.5" in text
    assert f"Full plan: {PLAN_PATH}" in text


def test_render_missing_sections_and_verify() -> None:
    plan = "# Plan\nDo it.\n\nSIZE: big\nJUDGMENT: needed\n"
    text = render_plan_view(_job(), "# Task\nGo.\n", plan, PLAN_PATH)
    assert "(none given)" in text
    assert text.count("(none given)") == 4
    assert "HOW IT WILL BE CHECKED\nno verify command" in text
    assert "SIZE: big   BUILDER: Grok 4.6 high (judgment needed)" in text


def test_render_revision_header_and_truncation() -> None:
    long_goal = "G" * 1600
    long_task = "T" * 1300
    plan = f"## GOAL\n{long_goal}\n\nSIZE: small\nJUDGMENT: none\nVERIFY: true\n"
    text = render_plan_view(
        _job(flags={"revisions": 2}),
        long_task,
        plan,
        PLAN_PATH,
    )
    assert "PLAN FOR APPROVAL  (revision 2 of 3)" in text
    task_block = text.split("TASK AS SENT TO CLAUDE\n", 1)[1].split("\n\nGOAL\n", 1)[0]
    assert task_block.endswith("…")
    assert len(task_block) == 1201
    goal_block = text.split("\nGOAL\n", 1)[1].split("\n\nWHAT WILL CHANGE\n", 1)[0]
    assert goal_block.endswith("…")
    assert len(goal_block) == 1501


def test_feedback_survives_task_truncation(tmp_path) -> None:
    from onyx_pipeline.planview import render_plan_view

    task = "x" * 3000 + "\n## Plan feedback (round 1)\nuse sqlite please\n"
    view = render_plan_view(_job(), task, "## GOAL\ng\nSIZE: small\nJUDGMENT: none", tmp_path / "p")
    assert "use sqlite please" in view


def test_split_plan_lists_each_part_builder(tmp_path) -> None:
    from onyx_pipeline.planview import render_plan_view

    plan = (
        "## PARTS\n"
        "PART 1: a | FILES: a.py | AFTER: none | JUDGMENT: none\nsteps\n"
        "PART 2: b | FILES: b.py | AFTER: 1 | JUDGMENT: needed\nsteps\n"
        "SIZE: small\nJUDGMENT: none\nVERIFY: true\n"
    )
    view = render_plan_view(_job(), "task", plan, tmp_path / "p")
    assert "part 1 Composer 2.5" in view
    assert "part 2 Grok 4.6 high" in view
