"""Tests for onyx-think playbooks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from onyx_pipeline.config import load_config
from onyx_pipeline.think import (
    NEXT_LINE,
    PlaybookReply,
    parse_playbook,
    parse_reply,
    render_playbook,
)
from onyx_pipeline.think import main as think_main

SAMPLE_BODY = (
    "## When to use\n"
    "When installing the foo tool.\n"
    "## Steps\n"
    "1. Run:\n"
    "```bash\n"
    "which foo\n"
    "```\n"
    "Output should show a path.\n"
    "## How to tell it worked\n"
    "foo runs.\n"
    "## If it fails\n"
    "Check PATH.\n"
    "## Not checked\n"
    "Nothing.\n"
)

SAMPLE_REPLY = (
    "PLAYBOOK\n"
    "TITLE: Install foo tool\n"
    "SLUG: install-foo-tool\n"
    "TRIGGERS: install foo; set up foo tool; foo package install\n"
    "ASK-FIRST: none\n"
    "CHECKED: yes\n"
    f"---\n{SAMPLE_BODY}"
)


@pytest.fixture
def playbooks_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pb = tmp_path / "playbooks"
    pb.mkdir()
    monkeypatch.setenv("ONYX_PLAYBOOKS_DIR", str(pb))
    monkeypatch.setenv("ONYX_AGENTS_MD", str(tmp_path / "AGENTS.md"))
    (tmp_path / "AGENTS.md").write_text("# Agents\n")
    cfg = load_config()
    calls: list[tuple[str, str]] = []

    def fake_think(_cfg, _log, _workspace, prompt: str) -> tuple[int, str]:
        calls.append(("think", prompt))
        return 0, SAMPLE_REPLY

    monkeypatch.setattr("onyx_pipeline.think.think_worker", fake_think)
    return pb, cfg, calls


def test_new_saves_playbook_and_logs_usage(playbooks_env, capsys):
    pb, _cfg, calls = playbooks_env
    code = think_main(["install the foo tool on this machine please"])
    assert code == 0
    out = capsys.readouterr().out
    assert "[onyx-think saved install-foo-tool]" in out
    assert NEXT_LINE.format(slug="install-foo-tool") in out
    path = pb / "install-foo-tool.md"
    assert path.is_file()
    text = path.read_text()
    assert "title: Install foo tool" in text
    assert "status: untested" in text
    assert "repairs: 0" in text
    assert "uses: 0" in text
    assert "Rules: AGENTS.md still applies." in text
    assert len(calls) == 1
    usage = (pb / ".usage.jsonl").read_text().strip().splitlines()
    assert len(usage) == 1
    row = json.loads(usage[0])
    assert row["kind"] == "new"
    assert row["ok"] is True


def test_parse_reply_with_preamble_and_fence(playbooks_env):
    wrapped = (
        "Here is my answer:\n"
        "```\n"
        "PLAYBOOK\n"
        "TITLE: Install foo tool\n"
        "SLUG: install-foo-tool\n"
        "TRIGGERS: a; b; c\n"
        "ASK-FIRST: none\n"
        "CHECKED: yes\n"
        f"---\n{SAMPLE_BODY}\n"
        "```"
    )
    reply = parse_reply(wrapped)
    assert isinstance(reply, PlaybookReply)
    assert reply.slug == "install-foo-tool"


def test_no_playbook_saves_nothing(playbooks_env, monkeypatch, capsys):
    pb, _cfg, _calls = playbooks_env

    def no_pb(_cfg, _log, _ws, _prompt):
        return 0, "NO-PLAYBOOK: not a procedure\nJust chat."

    monkeypatch.setattr("onyx_pipeline.think.think_worker", no_pb)
    code = think_main(["what is the meaning of life"])
    assert code == 0
    out = capsys.readouterr().out
    assert "[onyx-think no-playbook] not a procedure" in out
    assert "Just chat." in out
    assert list(pb.glob("*.md")) == []


def test_unparseable_reply_saved_to_history(playbooks_env, monkeypatch, capsys):
    pb, _cfg, _calls = playbooks_env

    def bad(_cfg, _log, _ws, _prompt):
        return 0, "random garbage"

    monkeypatch.setattr("onyx_pipeline.think.think_worker", bad)
    code = think_main(["do something new and unique xyz"])
    assert code == 1
    out = capsys.readouterr().out
    assert "[onyx-think failed]" in out
    assert "expected format" in out
    hist = list((pb / ".history").glob("unparsed-*.txt"))
    assert len(hist) == 1
    assert list(pb.glob("*.md")) == []


def test_worker_nonzero_exit(playbooks_env, monkeypatch, capsys):
    pb, _cfg, _calls = playbooks_env

    def fail(_cfg, _log, _ws, _prompt):
        return 1, ""

    monkeypatch.setattr("onyx_pipeline.think.think_worker", fail)
    code = think_main(["brand new unique task alpha beta gamma"])
    assert code == 1
    assert "[onyx-think failed]" in capsys.readouterr().out
    assert list(pb.glob("*.md")) == []


def test_similar_task_returns_existing_without_worker(playbooks_env, monkeypatch, capsys):
    pb, _cfg, calls = playbooks_env
    think_main(["install the foo tool on this machine please"])
    calls.clear()

    code = think_main(["install foo tool"])
    assert code == 0
    out = capsys.readouterr().out
    assert "[onyx-think existing install-foo-tool]" in out
    assert len(calls) == 0


def test_slug_collision_gets_suffix(playbooks_env, monkeypatch):
    pb, _cfg, _calls = playbooks_env
    think_main(["install the foo tool on this machine please"])

    def same_slug(_cfg, _log, _ws, _prompt):
        return 0, SAMPLE_REPLY

    monkeypatch.setattr("onyx_pipeline.think.think_worker", same_slug)
    think_main(["another totally different unique task qwerty"])
    assert (pb / "install-foo-tool.md").is_file()
    assert (pb / "install-foo-tool-2.md").is_file()


def test_privileged_body_auto_flags_ask_first(playbooks_env, monkeypatch, capsys):
    pb, _cfg, _calls = playbooks_env
    body = SAMPLE_BODY.replace("which foo", "sudo pacman -S foo")
    reply = SAMPLE_REPLY.replace(SAMPLE_BODY, body)

    def privileged(_cfg, _log, _ws, _prompt):
        return 0, reply

    monkeypatch.setattr("onyx_pipeline.think.think_worker", privileged)
    think_main(["unique privileged task request"])
    text = (pb / "install-foo-tool.md").read_text()
    assert "ask_first: contains privileged" in text
    assert "auto-flagged" in text


def test_daily_limit_blocks_second_call(playbooks_env, monkeypatch, capsys):
    pb, _cfg, calls = playbooks_env
    monkeypatch.setenv("ONYX_THINK_DAILY_LIMIT", "1")
    think_main(["unique task one zzz"])
    calls.clear()
    code = think_main(["unique task two zzz"])
    assert code == 3
    assert "[onyx-think limit]" in capsys.readouterr().out
    assert len(calls) == 0


def test_worked_sets_status_and_uses(playbooks_env, capsys):
    pb, _cfg, _calls = playbooks_env
    think_main(["install the foo tool on this machine please"])
    code = think_main(["worked", "install-foo-tool"])
    assert code == 0
    assert "[onyx-think worked install-foo-tool] noted (used 1 times)" in capsys.readouterr().out
    meta, _ = parse_playbook((pb / "install-foo-tool.md").read_text())
    assert meta["status"] == "works"
    assert meta["uses"] == "1"


def test_failed_repair_writes_history(playbooks_env, monkeypatch, capsys):
    pb, _cfg, _calls = playbooks_env
    think_main(["install the foo tool on this machine please"])
    repaired_body = SAMPLE_BODY + "\n## Extra\nfixed step.\n"
    repaired = SAMPLE_REPLY.replace(SAMPLE_BODY, repaired_body)

    def repair(_cfg, _log, _ws, _prompt):
        return 0, repaired

    monkeypatch.setattr("onyx_pipeline.think.think_worker", repair)
    code = think_main(["failed", "install-foo-tool", "step 1 said command not found"])
    assert code == 0
    out = capsys.readouterr().out
    assert "[onyx-think repaired install-foo-tool]" in out
    assert (pb / ".history" / "install-foo-tool-1.md").is_file()
    meta, body = parse_playbook((pb / "install-foo-tool.md").read_text())
    assert meta["repairs"] == "1"
    assert meta["status"] == "untested"
    assert meta["created"]
    assert "fixed step" in body


def test_failed_at_repair_cap(playbooks_env, monkeypatch, capsys):
    pb, _cfg, _calls = playbooks_env
    monkeypatch.setenv("ONYX_THINK_MAX_REPAIRS", "2")
    think_main(["install the foo tool on this machine please"])
    path = pb / "install-foo-tool.md"
    meta, body = parse_playbook(path.read_text())
    meta["repairs"] = "2"
    path.write_text(render_playbook(meta, body))

    def should_not_run(*_a):
        raise AssertionError("worker should not be called")

    monkeypatch.setattr("onyx_pipeline.think.think_worker", should_not_run)
    code = think_main(["failed", "install-foo-tool", "still broken"])
    assert code == 3
    assert "[onyx-think broken install-foo-tool]" in capsys.readouterr().out
    meta2, _ = parse_playbook(path.read_text())
    assert meta2["status"] == "broken"


def test_failed_unparseable_marks_broken(playbooks_env, monkeypatch, capsys):
    pb, _cfg, _calls = playbooks_env
    think_main(["install the foo tool on this machine please"])

    def bad(_cfg, _log, _ws, _prompt):
        return 0, "not a playbook"

    monkeypatch.setattr("onyx_pipeline.think.think_worker", bad)
    code = think_main(["failed", "install-foo-tool", "error"])
    assert code == 1
    assert "[onyx-think broken install-foo-tool]" in capsys.readouterr().out
    meta, _ = parse_playbook((pb / "install-foo-tool.md").read_text())
    assert meta["status"] == "broken"


def test_list_show_usage(playbooks_env, capsys):
    pb, _cfg, _calls = playbooks_env
    think_main(["install the foo tool on this machine please"])
    think_main(["worked", "install-foo-tool"])
    capsys.readouterr()

    think_main(["list"])
    assert "install-foo-tool  works  Install foo tool" in capsys.readouterr().out

    think_main(["show", "install-foo-tool"])
    assert "title: Install foo tool" in capsys.readouterr().out

    think_main(["usage"])
    usage_out = capsys.readouterr().out
    assert "thinker calls today:" in usage_out
    assert "last 30 days:" in usage_out
    assert "playbooks: 1 (1 works" in usage_out


def test_unknown_slug_exit_2(playbooks_env, capsys):
    code = think_main(["show", "missing-slug"])
    assert code == 2
    assert "no playbook named" in capsys.readouterr().err


def test_playbook_round_trip():
    meta = {
        "title": "T",
        "triggers": "a; b",
        "ask_first": "none",
        "checked": "yes",
        "status": "untested",
        "created": "2026-01-01",
        "updated": "2026-01-01",
        "repairs": "0",
        "uses": "0",
    }
    body = "## Steps\n1. Go\n"
    text = render_playbook(meta, body)
    meta2, body2 = parse_playbook(text)
    assert meta2["title"] == meta["title"]
    assert body2 == body.rstrip("\n")


def test_privileged_step_gets_marked_above_its_code_block():
    from onyx_pipeline.think import mark_privileged_steps

    body = "## Steps\n1.\n```\nsudo pacman -S foo\n```\nInstalls foo.\n2.\n```\nfoo --version\n```"
    marked, flagged = mark_privileged_steps(body)
    lines = marked.splitlines()
    assert flagged
    assert lines[lines.index("```") - 1].startswith("[ASK DAVID FIRST]")
    assert marked.count("[ASK DAVID FIRST]") == 1
    assert "```\nsudo pacman -S foo\n```" in marked

    already = "1. [ASK DAVID FIRST] run `sudo systemctl restart caddy`"
    assert mark_privileged_steps(already) == (already, False)


def test_fenced_reply_after_preamble_drops_closing_fence():
    wrapped = (
        "Here is my answer:\n```\nPLAYBOOK\nTITLE: Do a thing\nSLUG: do-a-thing\n"
        "TRIGGERS: a; b; c\nASK-FIRST: none\nCHECKED: yes\n---\n## Steps\n1. `ls`\n```"
    )
    reply = parse_reply(wrapped)
    assert isinstance(reply, PlaybookReply)
    assert reply.body == "## Steps\n1. `ls`"
