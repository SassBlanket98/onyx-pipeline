"""Shared fixtures for stub-driven pipeline tests."""

from __future__ import annotations

import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from onyx_pipeline.config import load_config

STUBS = Path(__file__).parent / "stubs"


def _make_executable(path: Path) -> None:
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def onyx_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    jobs = tmp_path / "jobs"
    research = tmp_path / "research"
    roots = tmp_path / "Projects"
    jobs.mkdir()
    research.mkdir()
    roots.mkdir()
    for name in ("cursor-agent", "claude", "codex", "openclaw"):
        _make_executable(STUBS / name)
    monkeypatch.setenv("ONYX_JOBS_DIR", str(jobs))
    monkeypatch.setenv("ONYX_RESEARCH_DIR", str(research))
    monkeypatch.setenv("ONYX_ALLOWED_ROOTS", str(roots))
    monkeypatch.setenv("ONYX_CURSOR_BIN", str(STUBS / "cursor-agent"))
    monkeypatch.setenv("ONYX_CLAUDE_BIN", str(STUBS / "claude"))
    monkeypatch.setenv("ONYX_CODEX_BIN", str(STUBS / "codex"))
    monkeypatch.setenv("ONYX_OPENCLAW_BIN", str(STUBS / "openclaw"))
    monkeypatch.setenv("ONYX_AUTO_APPROVE", "1")
    monkeypatch.setenv("ONYX_BUILD_BACKEND", "cursor")
    monkeypatch.setenv("ONYX_NO_NOTIFY", "1")
    monkeypatch.setenv("ONYX_DUP_WINDOW_MIN", "0")
    monkeypatch.setenv("ONYX_WORKER_TIMEOUT", "30")
    monkeypatch.setenv("ONYX_VERIFY_TIMEOUT", "30")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "onyx-job")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "onyx-job@local")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "onyx-job")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "onyx-job@local")
    monkeypatch.delenv("STUB_CLAUDE_MODE", raising=False)
    monkeypatch.delenv("STUB_REVIEW", raising=False)
    monkeypatch.delenv("STUB_BUILD_TOUCH", raising=False)
    monkeypatch.delenv("STUB_VERIFY_CMD", raising=False)
    monkeypatch.delenv("STUB_JUDGMENT", raising=False)
    monkeypatch.delenv("STUB_NO_VERIFY", raising=False)
    monkeypatch.delenv("STUB_PLAN_MODE", raising=False)
    monkeypatch.delenv("STUB_FAIL_PART", raising=False)
    monkeypatch.delenv("STUB_BUILD_SLEEP", raising=False)
    monkeypatch.delenv("STUB_TIMES_FILE", raising=False)
    monkeypatch.setenv("STUB_VERIFY_CMD", "true")
    return SimpleNamespace(
        jobs=jobs,
        research=research,
        roots=roots,
        cfg=load_config(),
        tmp=tmp_path,
    )
