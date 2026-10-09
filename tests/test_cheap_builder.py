"""The cheap OpenCode builder: who builds what, and how it is launched."""

from __future__ import annotations

import stat
from pathlib import Path

from onyx_pipeline.config import load_config
from onyx_pipeline.planview import render_plan_view
from onyx_pipeline.workers import COMPOSER_MODEL, GROK_MODEL, build, build_model_for


def test_build_model_for(monkeypatch):
    monkeypatch.setenv("ONYX_BUILD_BACKEND", "opencode")
    monkeypatch.setenv("ONYX_BUILD_MODEL", "opencode-go/glm-5.3-flash")
    cfg = load_config()
    assert build_model_for(cfg, "none") == "opencode-go/glm-5.3-flash"
    assert build_model_for(cfg, "needed") == GROK_MODEL

    monkeypatch.setenv("ONYX_BUILD_BACKEND", "cursor")
    assert build_model_for(load_config(), "none") == COMPOSER_MODEL

    # Grok never goes through OpenCode, whatever the config says.
    monkeypatch.setenv("ONYX_BUILD_BACKEND", "opencode")
    monkeypatch.setenv("ONYX_BUILD_MODEL", "opencode-go/grok-4.6")
    assert build_model_for(load_config(), "none") == COMPOSER_MODEL


def test_opencode_build_runs_in_the_repo(tmp_path: Path, monkeypatch):
    stub = tmp_path / "opencode"
    stub.write_text('#!/bin/sh\necho "$PWD" > seen_pwd\necho "$@" > seen_args\n')
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    repo = tmp_path / "repo"
    repo.mkdir()
    job_directory = tmp_path / "job"
    job_directory.mkdir()
    monkeypatch.setenv("ONYX_OPENCODE_BIN", str(stub))
    monkeypatch.setenv("PWD", str(tmp_path))  # a stale PWD must not leak into the worker
    cfg = load_config()

    code, _out = build(cfg, job_directory, repo, "opencode-go/glm-5.3-flash", "do the plan")

    assert code == 0
    assert (repo / "seen_pwd").read_text().strip() == str(repo)
    assert (repo / "seen_args").read_text().split()[:5] == [
        "run",
        "--standalone",
        "-m",
        "opencode-go/glm-5.3-flash",
        "--auto",
    ]


def test_plan_view_names_the_cheap_builder(tmp_path: Path):
    from onyx_pipeline.jobs import Job

    job = Job.__new__(Job)
    job.flags = {}
    job.id = "j1"
    plan = "## GOAL\ng\nSIZE: small\nJUDGMENT: none"
    view = render_plan_view(
        job, "task", plan, tmp_path / "p", cheap_model="opencode-go/glm-5.3-flash"
    )
    assert "BUILDER: glm-5.3-flash (OpenCode)" in view
