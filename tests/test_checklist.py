"""Planned-file checklist for live progress."""

from __future__ import annotations

from onyx_pipeline.jobs import Job, create_job
from onyx_pipeline.parse import planned_files
from onyx_pipeline.progress import _format_text, start_checklist, tick_checklist


def test_planned_files_from_parts_and_changes() -> None:
    plan = (
        "# Plan\n"
        "## CHANGES\n"
        "- `src/foo.py` — update handler\n"
        "- prose only bullet\n"
        "- lib/bar.py - tweak\n"
        "\n"
        "## PARTS\n"
        "PART 1: One | FILES: a.py, b.py | AFTER: none | JUDGMENT: none\n"
        "steps\n"
        "SIZE: small\n"
        "JUDGMENT: none\n"
        "VERIFY: true\n"
    )
    files = planned_files(plan)
    assert files == ["a.py", "b.py", "src/foo.py", "lib/bar.py"]


def test_tick_checklist_updates_and_skips_junk(monkeypatch, onyx_env) -> None:
    cfg = onyx_env.cfg
    job = create_job(cfg, kind="code", task="t", repo=None)
    start_checklist(job, ["a.py", "b.py"])
    saves: list[int] = []
    updates: list[int] = []

    monkeypatch.setattr("onyx_pipeline.progress.save_job", lambda _c, _j: saves.append(1))
    monkeypatch.setattr(
        "onyx_pipeline.progress.update_progress",
        lambda _c, _j: updates.append(1),
    )
    tick_checklist(cfg, job, [])
    assert saves == []
    tick_checklist(cfg, job, ["a.py", "__pycache__/x.pyc", "extra.py"])
    cl = job.flags["checklist"]
    assert cl["files"]["a.py"] is True
    assert cl["files"]["b.py"] is False
    assert cl["extra"] == ["extra.py"]
    assert saves == [1]
    assert updates == [1]
    tick_checklist(cfg, job, ["a.py"])
    assert len(saves) == 1
    tick_checklist(cfg, job, ["b.py", "b.py"])
    assert cl["files"]["b.py"] is True
    assert len(saves) == 2


def test_format_text_checklist_and_truncation() -> None:
    job = Job.__new__(Job)
    job.kind = "code"
    job.task = "Build thing"
    job.state = "building"
    job.stages = [{"state": "building", "at": "2026-10-03T14:30:00+00:00"}]
    job.fix_rounds_used = 0
    job.build_model = "composer-2.5-fast"
    job.flags = {
        "checklist": {
            "files": {f"f{i}.py": i == 0 for i in range(15)},
            "extra": ["z.py"],
            "at": "15:01",
        }
    }
    text = _format_text(job)
    assert "building since" in text
    assert "Composer 2.5" in text
    assert "✅ f0.py" in text
    assert "⬜ f1.py" in text
    assert "… and 3 more (1 done)" in text
    assert "+1 other file(s) touched" in text
    assert "last change 15:01" in text

    job.state = "verifying"
    plain = _format_text(job)
    assert "✅" not in plain
    assert plain.count("\n") == 1


def test_start_checklist_keeps_ticks() -> None:
    job = Job.__new__(Job)
    job.flags = {
        "checklist": {
            "files": {"a.py": True},
            "extra": ["x.py"],
            "at": "12:00",
        }
    }
    start_checklist(job, ["a.py", "b.py"])
    cl = job.flags["checklist"]
    assert cl["files"]["a.py"] is True
    assert cl["files"]["b.py"] is False
    assert cl["extra"] == ["x.py"]
    assert cl["at"] == "12:00"
