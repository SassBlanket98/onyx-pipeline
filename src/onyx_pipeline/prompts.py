"""Prompt and brief text for each worker stage."""

from __future__ import annotations

from pathlib import Path

OPS_BRIEF_SCHEMA = (
    '{"version":1,"summary":"<one or two sentences>",'
    '"steps":[{"number":1,"action":"<what to do>","state_change":true,'
    '"exact_values":"<the literal values needed, or none>",'
    '"verification":"<how to confirm the step worked>"}],'
    '"changes":["<number and action of every state-changing step>"]}'
)

LIMITS_BLOCK = """\
Limits:
- no sudo, no installs, no services or config;
- nothing outside the repo;
- no deletes outside the diff;
- no pushes, nothing sent or published;
- git: stay on the current branch; do not create or switch branches, commit, amend,
  reset, rebase or stash. The pipeline commits your changes for you;
- stop and report if the plan needs any of those."""


def investigate_brief(task: str, *, research: bool = False) -> str:
    if research:
        return (
            "Gather the facts this task needs. Include sources with links. "
            "Do not make any changes. Output markdown.\n\n"
            f"Task:\n{task}\n"
        )
    return (
        "Gather the facts this task needs from the repository, with file:line "
        "for every claim. Do not make any changes. Output markdown.\n\n"
        f"Task:\n{task}\n"
    )


def plan_prompt(task: str, evidence: str) -> str:
    return (
        "Using the task and the evidence below, write a step-by-step implementation "
        "plan with the decisions made and the reasons for them. Do not edit any files; "
        "your final message is the plan.\n\n"
        "Separate Cursor workers build the plan, and each should see as little as "
        "possible. So whenever the work has two or more pieces that edit different files, "
        "add a ## PARTS section after the plan body and before the three trailer lines, "
        "even when the job is small:\n\n"
        "## PARTS\n"
        "PART 1: <short title> | FILES: path/a.py, path/b.py | AFTER: none | JUDGMENT: none\n"
        "<the steps for this part only>\n"
        "PART 2: <title> | FILES: path/c.py | AFTER: 1 | JUDGMENT: needed\n"
        "<steps>\n\n"
        "Rules:\n"
        "- Split whenever two or more pieces edit different files. Give each file to "
        "exactly one part; a part that needs another part's code lists it in AFTER.\n"
        "- Tests for a piece belong to the same part as that piece.\n"
        "- At most 3 parts.\n"
        "- AFTER names parts that must be finished first: none, or comma-separated numbers.\n"
        "- Leave out ## PARTS only when the change is one tightly-knit piece (for "
        "example a single file, or edits that all depend on each other).\n\n"
        "Before ## PARTS, include these sections:\n\n"
        "## GOAL\n"
        "<one or two sentences: what will exist when this is done>\n"
        "## CHANGES\n"
        "- <path> — <what changes in it>   (one bullet per file or part)\n"
        "## OUT OF SCOPE\n"
        "- <things you deliberately will not touch>\n"
        "## ASSUMPTIONS AND DEVIATIONS\n"
        "- <anything where the plan departs from the task as written, or assumes "
        'something the task didn\'t say; write "none" if none>\n\n'
        "End with exactly these three lines:\n"
        "SIZE: small|big\n"
        "JUDGMENT: none|needed\n"
        "VERIFY: <cmd1> && <cmd2>\n\n"
        "VERIFY commands run from the project root. Never use cd or absolute paths in them.\n\n"
        "Big means more than about 30 minutes of agent work or touching more than "
        "about 8 files.\n\n"
        "You may add a ## OPEN QUESTIONS section listing only questions that block "
        "the work.\n\n"
        f"## Task\n{task}\n\n## Evidence\n{evidence}\n"
    )


def build_brief(plan: str) -> str:
    return f"{plan.rstrip()}\n\n{LIMITS_BLOCK}\n"


def part_brief(
    *,
    n: int,
    title: str,
    steps: str,
    files: list[str],
    others: list[tuple[str, list[str]]],
    decisions: str,
    extra: str = "",
) -> str:
    owned = ", ".join(files)
    other_line = "; ".join(f"{name} ({', '.join(fs)})" for name, fs in others) or "none"
    decision_text = "\n".join(decisions.splitlines()[:60])
    extra_block = f"{extra}\n\n" if extra else ""
    return (
        f"# Part {n}: {title}\n\n"
        f"{steps.rstrip()}\n\n"
        f"only edit these files: {owned}\n"
        f"other workers own these; don't touch them: {other_line}\n\n"
        f"{extra_block}"
        f"{decision_text}\n\n"
        f"{LIMITS_BLOCK}\n"
    )


def fix_brief(plan: str, review: str, verify_log: str) -> str:
    return (
        "Fix the issues described in review.md and verify.log. Follow the plan.\n\n"
        f"{LIMITS_BLOCK}\n\n"
        "## Plan\n"
        f"{plan}\n\n"
        "## Review\n"
        f"{review}\n\n"
        "## Verify log\n"
        f"{verify_log}\n"
    )


_NO_SANDBOX_NOTE = (
    "These commands were already run outside your sandbox with the results above. "
    "Do not run tests yourself and do not fail the review because you cannot run them. "
    "Judge the code and the test coverage."
)


def _verify_block(verify_results: list[dict[str, object]] | None) -> str:
    lines: list[str] = []
    for item in verify_results or []:
        if not item.get("command"):
            continue
        line = f"- `{item['command']}` — exit {item.get('exit_code')}"
        if item.get("could_not_run"):
            line += " — could not run (environment problem, not a test failure)"
        lines.append(line)
        tail = item.get("tail")
        if tail and item.get("exit_code") != 0:
            lines.extend(f"    {ln}" for ln in str(tail).splitlines())
    listing = "\n".join(lines) if lines else "no verify commands ran"
    return f"Verify results (already run outside your sandbox):\n{listing}\n\n{_NO_SANDBOX_NOTE}"


def review_prompt(
    plan: str, base: str, verify_results: list[dict[str, object]] | None = None
) -> str:
    return (
        f"Review the change on this branch: run `git diff {base}...HEAD` to see it. "
        "Check it against the plan below. Report real bugs only, not style. "
        "Do not edit anything. End with exactly one line: VERDICT: PASS or VERDICT: FAIL.\n\n"
        f"{_verify_block(verify_results)}\n\n"
        f"Plan:\n{plan}\n"
    )


def research_report_prompt(task: str, evidence: str) -> str:
    return (
        "Using the task and the evidence below, write the research report in markdown. "
        "Put the verdict first. Keep the sources as links. Do not edit any files; your "
        "final message is the report.\n\n"
        f"## Task\n{task}\n\n## Evidence\n{evidence}\n"
    )


def fact_check_prompt(report_path: Path) -> str:
    return (
        "Fact-check the research report. Do not use the author's hidden reasoning; "
        "check the claims only. List each claim that is wrong with the correction and "
        "where you checked it. A claim you could not check (no network, private page) "
        "is unverified, not wrong: list it separately and do not count it against the "
        "report.\n"
        "Choose the verdict:\n"
        "- PASS: no claim is wrong.\n"
        "- CORRECTIONS: the conclusion and most claims hold, but the claims you listed "
        "are wrong and need the corrections you gave.\n"
        "- FAIL: the main conclusion is wrong, or most of the claims you could check "
        "are wrong.\n"
        "The last line of your answer must be exactly VERDICT: PASS, "
        "VERDICT: CORRECTIONS or VERDICT: FAIL, with nothing after it.\n\n"
        f"Report path: {report_path}\n"
    )


def ops_brief_prompt(task: str) -> str:
    return (
        "Write the operator brief for the raw request below. You cannot change "
        "anything: you have read-only tools and your final message is the brief.\n\n"
        "First write the brief body in markdown: a short summary, then the numbered "
        "steps in the order they must run. Then output exactly one fenced json block, "
        "opening with ```json and closing with ```, and put nothing after it. Use "
        "exactly this schema:\n\n"
        "```json\n"
        f"{OPS_BRIEF_SCHEMA}\n"
        "```\n\n"
        "Rules:\n"
        "- Number the steps from 1, in execution order.\n"
        "- state_change is true for any step that alters system state (writes, sends, "
        "posts, config, DNS, files) and false for read-only steps.\n"
        "- exact_values must hold the literal values the operator needs: names, URLs, "
        'selectors, addresses, commands. Never write "the usual value".\n'
        "- Every state-changing step must be listed in changes, by number and action. "
        "changes must not be empty; if the request truly needs no state change, say so "
        "in the body instead of inventing one.\n"
        "- Keep the body and the json block consistent.\n\n"
        f"## Request\n{task}\n"
    )


def ops_operate_prompt(brief_md: str) -> str:
    return (
        "Carry out the operator brief below in the browser, exactly as written. You "
        "have the Chrome tools and your final message is the report of what you did.\n\n"
        "Rules:\n"
        "- Follow the numbered steps in order, exactly as written.\n"
        "- Change nothing that is not in the brief.\n"
        "- Stop and report at once if a page differs from what the brief expects; do "
        "not improvise around it.\n"
        "- Never enter a password, and never ask for one.\n"
        "- Finish with a list of everything you changed.\n\n"
        f"## Brief\n{brief_md}\n"
    )


def ops_verify_prompt(brief: dict) -> str:
    steps = brief.get("steps")
    step_lines = []
    if isinstance(steps, list):
        for step in steps:
            if not isinstance(step, dict):
                continue
            number = step.get("number")
            verification = step.get("verification", "")
            step_lines.append(f"{number}. {verification}")
    checks = "\n".join(step_lines) or "(no steps)"
    changes = brief.get("changes")
    change_lines = []
    if isinstance(changes, list):
        change_lines = [f"- {change}" for change in changes]
    change_text = "\n".join(change_lines) or "(no changes listed)"
    return (
        "Verify, read-only, the changes an operator just made in the browser. You may "
        "only look things up: run the checks below and read files. Do not change "
        "anything.\n\n"
        "Check each step's verification field against the live system. Then choose the "
        "verdict:\n"
        "- confirmed: every check passed and the changes are in place.\n"
        "- uncertain: the checks could not prove the changes took effect (a check was "
        "unavailable, or the result was unclear).\n"
        "- failed: a check shows a change did not take effect, or took the wrong value.\n\n"
        "The first line of your answer must be exactly one of:\n"
        "RESULT: confirmed\n"
        "RESULT: uncertain\n"
        "RESULT: failed\n"
        "Put the per-step evidence in the markdown after that line.\n\n"
        f"## Changes to confirm\n{change_text}\n\n"
        f"## Checks\n{checks}\n"
    )
