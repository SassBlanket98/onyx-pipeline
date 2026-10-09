"""CLI argument checks (no detached runner)."""

from __future__ import annotations

import pytest

from onyx_pipeline.cli import run


def test_start_code_requires_repo(onyx_env, capsys):
    code = run(["start", "--kind", "code", "do the work now please"])
    assert code == 2
    err = capsys.readouterr().err
    assert "--repo" in err


def test_start_prints_id_without_spawn(onyx_env, monkeypatch, capsys):
    monkeypatch.setattr("onyx_pipeline.cli.spawn_runner", lambda cfg: None)
    repo = onyx_env.roots / "cli-app"
    repo.mkdir()
    code = run(["start", "--kind", "code", "--repo", str(repo), "Add a tiny helper function"])
    assert code == 0
    job_id = capsys.readouterr().out.strip()
    assert job_id
    assert (onyx_env.jobs / job_id / "state.json").exists()


def test_help_hides_run(capsys):
    with pytest.raises(SystemExit) as exc:
        run(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "_run" not in out
    assert "start" in out


def _two_jobs(onyx_env, monkeypatch, capsys):
    from datetime import datetime

    from onyx_pipeline import jobs

    make = jobs.make_job_id
    fixed = datetime(2026, 10, 7, 14, 45, 27)
    monkeypatch.setattr(
        jobs, "make_job_id", lambda task, jobs_dir, now=None: make(task, jobs_dir, fixed)
    )
    monkeypatch.setattr("onyx_pipeline.cli.spawn_runner", lambda cfg: None)
    repo = onyx_env.roots / "short-app"
    repo.mkdir(exist_ok=True)
    ids = []
    for task in ("Add a tiny helper function", "Add a tiny helper function"):
        assert run(["start", "--kind", "code", "--repo", str(repo), task]) == 0
        ids.append(capsys.readouterr().out.strip())
    return ids


def test_short_id_resolves_when_unique(onyx_env, monkeypatch, capsys):
    first, second = _two_jobs(onyx_env, monkeypatch, capsys)
    assert second.startswith(first)
    assert run(["status", second[:-1]]) == 0
    assert capsys.readouterr().out.startswith(second + " ")
    assert run(["status", first]) == 0
    assert capsys.readouterr().out.startswith(first + " ")


def test_short_id_ambiguous_or_unknown(onyx_env, monkeypatch, capsys):
    first, second = _two_jobs(onyx_env, monkeypatch, capsys)
    assert run(["status", first[:15]]) == 2
    err = capsys.readouterr().err
    assert first in err and second in err
    assert run(["status", "19990101-000000"]) == 1
    assert "onyx-job list" in capsys.readouterr().err


def test_short_id_is_a_literal_prefix(onyx_env, monkeypatch, capsys):
    _two_jobs(onyx_env, monkeypatch, capsys)
    for bad in ("*", "?" * 15, "[0-9]"):
        assert run(["status", bad]) == 1
        assert "unknown job" in capsys.readouterr().err


def test_short_id_for_answer_and_revise(onyx_env, monkeypatch, capsys):
    from onyx_pipeline.cli import _resolve_id_and_text

    first, second = _two_jobs(onyx_env, monkeypatch, capsys)
    assert _resolve_id_and_text([second[:-1], "go", "on"], state="needs-answer") == (
        second,
        "go on",
    )
    assert _resolve_id_and_text([first, "ok"], state="needs-answer") == (first, "ok")
    for bad, msg in ((first[:15], "matches 2 jobs"), ("19990101", "unknown job")):
        with pytest.raises(ValueError, match=msg):
            _resolve_id_and_text([bad, "ok"], state="needs-answer")
