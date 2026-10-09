"""Operator brief: prompt, read-only worker launch, and structured-output parsing."""

from __future__ import annotations

import copy
import json
import stat
from pathlib import Path

import pytest

from onyx_pipeline.config import load_config
from onyx_pipeline.ops_brief import parse_brief, render_operator_brief
from onyx_pipeline.prompts import ops_brief_prompt
from onyx_pipeline.workers import brief_ops

VALID = {
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


def _brief_text(payload: dict | None = None, body: str = "# Brief\nDo the thing.\n") -> str:
    return f"{body}\n```json\n{json.dumps(payload if payload is not None else VALID)}\n```\n"


# --- prompt -----------------------------------------------------------------


def test_ops_brief_prompt_asks_for_body_then_one_json_block():
    prompt = ops_brief_prompt("Point example.com at the new host")
    assert "Point example.com at the new host" in prompt
    assert "```json" in prompt
    assert prompt.count("```") >= 2
    assert '"version":1' in prompt
    assert '"changes"' in prompt
    assert "state-changing" in prompt
    assert "changes must not be empty" in prompt


# --- parse_brief ------------------------------------------------------------


def test_parse_brief_returns_schema_and_keeps_exact_values():
    parsed = parse_brief(_brief_text())
    assert parsed == VALID
    assert parsed["steps"][1]["exact_values"] == "A=203.0.113.10"
    assert parsed["steps"][0]["state_change"] is False
    assert parsed["steps"][1]["state_change"] is True


def test_parse_brief_ignores_a_body_code_fence():
    text = "# Brief\nRun:\n\n```bash\ncurl example.com\n```\n\n" + _brief_text()
    assert parse_brief(text) == VALID


def test_parse_brief_rejects_missing_json_block():
    with pytest.raises(ValueError, match="no fenced json block"):
        parse_brief("# Brief\nNo structured part here.\n")


def test_parse_brief_rejects_malformed_json():
    with pytest.raises(ValueError):
        parse_brief("# Brief\n```json\n{not valid json}\n```\n")


def test_parse_brief_rejects_non_object():
    with pytest.raises(ValueError, match="must be an object"):
        parse_brief("# Brief\n```json\n[1, 2, 3]\n```\n")


def test_parse_brief_rejects_empty_steps():
    payload = copy.deepcopy(VALID)
    payload["steps"] = []
    with pytest.raises(ValueError, match="steps are empty"):
        parse_brief(_brief_text(payload))


def test_parse_brief_rejects_empty_changes():
    payload = copy.deepcopy(VALID)
    payload["changes"] = []
    with pytest.raises(ValueError, match="changes are empty"):
        parse_brief(_brief_text(payload))


def test_parse_brief_rejects_missing_changes():
    payload = copy.deepcopy(VALID)
    del payload["changes"]
    with pytest.raises(ValueError, match="changes are empty"):
        parse_brief(_brief_text(payload))


def test_parse_brief_rejects_step_missing_a_field():
    payload = copy.deepcopy(VALID)
    del payload["steps"][0]["verification"]
    with pytest.raises(ValueError, match="missing verification"):
        parse_brief(_brief_text(payload))


# --- render_operator_brief --------------------------------------------------


def test_render_operator_brief_numbers_steps_with_values_and_verification():
    rendered = render_operator_brief(VALID)
    assert VALID["summary"] in rendered
    for step in VALID["steps"]:
        assert f"{step['number']}. {step['action']}" in rendered
        assert step["exact_values"] in rendered
        assert step["verification"] in rendered
    # The prose brief is not part of the operator instruction.
    assert "```json" not in rendered


# --- brief_ops --------------------------------------------------------------


def _cfg_with_stub(tmp_path: Path, monkeypatch, reply: str):
    """A claude stand-in that records its argv and whatever reaches stdin."""
    stub = tmp_path / "claude-stub"
    args_file = tmp_path / "args.txt"
    stdin_file = tmp_path / "stdin.txt"
    reply_file = tmp_path / "reply.txt"
    reply_file.write_text(reply)
    stub.write_text(
        f'#!/bin/sh\ncat > "{stdin_file}"\nprintf "%s" "$*" > "{args_file}"\ncat "{reply_file}"\n'
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("ONYX_CLAUDE_BIN", str(stub))
    return load_config(), args_file, stdin_file


def test_brief_ops_launches_read_only_claude_with_prompt_as_argument(tmp_path, monkeypatch):
    reply = _brief_text()
    cfg, args_file, stdin_file = _cfg_with_stub(tmp_path, monkeypatch, reply)
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    task = "Point example.com at the new host"

    code, out = brief_ops(cfg, job_dir, task)

    assert code == 0
    assert out == reply
    args = args_file.read_text()
    assert "--model claude-sonnet-5-5" in args
    assert "--allowedTools Read,Grep,Glob" in args
    assert "--disallowedTools Edit,Write,Bash,NotebookEdit" in args
    assert task in args
    assert ops_brief_prompt(task) in args  # the prompt is an argument, not stdin
    assert stdin_file.read_text() == ""  # stdin is closed: cat saw EOF at once
    assert parse_brief(out) == VALID
    assert "exit 0" in (job_dir / "brief.log").read_text()
