"""Ops worker side: operate in Chrome, verify read-only, and progress ticks."""

from __future__ import annotations

import stat
from pathlib import Path

from onyx_pipeline.config import load_config
from onyx_pipeline.jobs import create_job, transition
from onyx_pipeline.progress import _format_text
from onyx_pipeline.prompts import ops_operate_prompt, ops_verify_prompt
from onyx_pipeline.workers import (
    OPS_CHROME_TOOLS,
    OPS_MODEL,
    OPS_VERIFY_TOOLS,
    operate_ops,
    ops_work_env,
    verify_ops,
)

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
BRIEF_MD = "# Operator brief\n\n1. Set example.com to 203.0.113.10\n"


def _stub(tmp_path: Path, reply: str, *, exit_code: int = 0):
    """A claude stand-in that records argv (one per line), stdin, and CLAUDE_CONFIG_DIR."""
    base = tmp_path / "claude-stub"
    args_file = tmp_path / "args.txt"
    stdin_file = tmp_path / "stdin.txt"
    env_file = tmp_path / "env.txt"
    reply_file = tmp_path / "reply.txt"
    reply_file.write_text(reply)
    base.write_text(
        "#!/bin/sh\n"
        f'cat > "{stdin_file}"\n'
        'for a in "$@"; do printf "%s\\n" "$a"; done > '
        f'"{args_file}"\n'
        f'printf "%s" "$CLAUDE_CONFIG_DIR" > "{env_file}"\n'
        f'cat "{reply_file}"\n'
        f"exit {exit_code}\n"
    )
    base.chmod(base.stat().st_mode | stat.S_IXUSR)
    return base, args_file, stdin_file, env_file


# --- prompts -----------------------------------------------------------------


def test_ops_operate_prompt_carries_the_brief_and_the_rules():
    prompt = ops_operate_prompt(BRIEF_MD)
    assert BRIEF_MD.rstrip() in prompt
    assert "Change nothing that is not in the brief" in prompt
    assert "Stop and report at once if a page differs" in prompt
    assert "Never enter a password" in prompt
    assert "list of everything you changed" in prompt


def test_ops_verify_prompt_lists_each_check_and_the_result_lines():
    prompt = ops_verify_prompt(BRIEF)
    assert "dig example.com +short" in prompt
    assert "2: set A=203.0.113.10 for example.com" in prompt
    assert "RESULT: confirmed" in prompt
    assert "RESULT: uncertain" in prompt
    assert "RESULT: failed" in prompt
    assert "first line" in prompt


# --- operate_ops -------------------------------------------------------------


def test_operate_ops_launches_chrome_claude_with_brief_as_argument(tmp_path, monkeypatch):
    reply = "changed the A record\n"
    base, args_file, stdin_file, env_file = _stub(tmp_path, reply)
    monkeypatch.setenv("ONYX_CLAUDE_BIN", str(base))
    cfg = load_config()
    job_dir = tmp_path / "job"
    job_dir.mkdir()

    code, out = operate_ops(cfg, job_dir, BRIEF_MD)

    assert code == 0
    assert out == reply
    raw = args_file.read_text()
    args = raw.splitlines()
    assert cfg.claude_bin == str(base)
    assert args[0] == "--chrome"
    assert "--chrome" in args
    assert args[args.index("--model") + 1] == OPS_MODEL
    assert f"--allowedTools={OPS_CHROME_TOOLS}" in args
    assert raw.endswith("-p\n" + ops_operate_prompt(BRIEF_MD) + "\n")  # prompt final after -p
    assert stdin_file.read_text() == ""  # DEVNULL: cat saw EOF at once
    assert env_file.read_text() == str(Path.home() / ".claude-work")
    assert "exit 0" in (job_dir / "operate.log").read_text()


def test_operate_ops_uses_only_the_chrome_allowlist(tmp_path, monkeypatch):
    base, args_file, _stdin, _env = _stub(tmp_path, "done\n")
    monkeypatch.setenv("ONYX_CLAUDE_BIN", str(base))
    cfg = load_config()
    job_dir = tmp_path / "job"
    job_dir.mkdir()

    operate_ops(cfg, job_dir, BRIEF_MD)

    args = args_file.read_text().splitlines()
    allow = next(a for a in args if a.startswith("--allowedTools=")).split("=", 1)[1]
    assert allow == "mcp__claude-in-chrome__*"
    assert "Edit" not in allow and "Write" not in allow


# --- verify_ops --------------------------------------------------------------


def test_verify_ops_is_read_only_and_keeps_the_result_line(tmp_path, monkeypatch):
    reply = "RESULT: confirmed\n\n1. dig example.com returned 203.0.113.10\n"
    base, args_file, stdin_file, env_file = _stub(tmp_path, reply)
    monkeypatch.setenv("ONYX_CLAUDE_BIN", str(base))
    cfg = load_config()
    job_dir = tmp_path / "job"
    job_dir.mkdir()

    code, report = verify_ops(cfg, job_dir, BRIEF)

    assert code == 0
    assert report.splitlines()[0] == "RESULT: confirmed"
    raw = args_file.read_text()
    args = raw.splitlines()
    assert "--chrome" not in args  # verification never drives the browser
    assert f"--allowedTools={OPS_VERIFY_TOOLS}" in args
    allow = next(a for a in args if a.startswith("--allowedTools=")).split("=", 1)[1]
    assert allow == "Read,Bash(dig *),Bash(curl -sI *)"
    assert "Bash(dig *)" in allow
    assert "Bash(curl -sI *)" in allow
    assert "Bash(curl -s *)" not in allow
    assert "Read" in allow
    assert "Edit" not in allow and "Write" not in allow
    assert raw.endswith(ops_verify_prompt(BRIEF) + "\n")  # prompt is the final positional
    assert stdin_file.read_text() == ""
    assert env_file.read_text() == str(Path.home() / ".claude-work")
    assert "exit 0" in (job_dir / "verify.log").read_text()


def test_verify_ops_without_a_verdict_is_uncertain(tmp_path, monkeypatch):
    base, _args, _stdin, _env = _stub(tmp_path, "I looked but forgot the line.\n")
    monkeypatch.setenv("ONYX_CLAUDE_BIN", str(base))
    cfg = load_config()
    job_dir = tmp_path / "job"
    job_dir.mkdir()

    code, report = verify_ops(cfg, job_dir, BRIEF)

    assert code == 0
    assert report.splitlines()[0] == "RESULT: uncertain"


def test_verify_ops_crash_is_failed_not_confirmed(tmp_path, monkeypatch):
    base, _args, _stdin, _env = _stub(tmp_path, "", exit_code=3)
    monkeypatch.setenv("ONYX_CLAUDE_BIN", str(base))
    cfg = load_config()
    job_dir = tmp_path / "job"
    job_dir.mkdir()

    code, report = verify_ops(cfg, job_dir, BRIEF)

    assert code == 3
    assert report.splitlines()[0] == "RESULT: failed"


def test_ops_work_env_points_at_the_dedicated_config_dir():
    assert ops_work_env() == {"CLAUDE_CONFIG_DIR": str(Path.home() / ".claude-work")}


# --- progress ticks ----------------------------------------------------------


def test_ops_progress_ticks_each_stage_as_it_is_reached(onyx_env):
    cfg = onyx_env.cfg
    job = create_job(cfg, kind="ops", task="restart the web server", repo=None)

    text = _format_text(job)
    assert "⬜ briefing" in text
    assert "⬜ waiting for approval" in text
    assert "⬜ operating" in text
    assert "⬜ verifying" in text

    transition(cfg, job, "briefing")
    text = _format_text(job)
    assert "✅ briefing" in text
    assert "⬜ waiting for approval" in text

    transition(cfg, job, "needs-approval")
    text = _format_text(job)
    assert "✅ waiting for approval" in text
    assert "⬜ operating" in text

    transition(cfg, job, "operating")
    text = _format_text(job)
    assert "✅ operating" in text
    assert "⬜ verifying" in text

    transition(cfg, job, "verifying")
    text = _format_text(job)
    assert "✅ verifying" in text


def test_code_jobs_do_not_show_ops_ticks(onyx_env):
    job = create_job(onyx_env.cfg, kind="code", task="Build the thing", repo=None)
    transition(onyx_env.cfg, job, "building")

    text = _format_text(job)

    assert "briefing" not in text
    assert "waiting for approval" not in text
    assert "operating" not in text
    assert "verifying" not in text
