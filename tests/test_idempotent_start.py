"""Idempotent `start --request-id`: one job, one spawn per request id."""

from __future__ import annotations

import threading

from onyx_pipeline.cli import run
from onyx_pipeline.jobs import create_job_idempotent, list_jobs, load_job


def test_repeat_request_id_returns_original_id_and_spawns_once(onyx_env, monkeypatch, capsys):
    spawns: list[int] = []
    monkeypatch.setattr("onyx_pipeline.cli.spawn_runner", lambda cfg: spawns.append(1))
    argv = ["start", "--kind", "research", "--request-id", "req_abc", "Look into widgets"]

    assert run(argv) == 0
    first = capsys.readouterr().out.strip()
    assert run(argv) == 0
    second = capsys.readouterr().out.strip()

    assert first
    assert first == second
    assert len(spawns) == 1
    assert [job.id for job in list_jobs(onyx_env.cfg)] == [first]
    assert load_job(onyx_env.cfg, first).request_id == "req_abc"


def test_different_request_ids_create_separate_jobs(onyx_env, monkeypatch, capsys):
    spawns: list[int] = []
    monkeypatch.setattr("onyx_pipeline.cli.spawn_runner", lambda cfg: spawns.append(1))

    assert run(["start", "--kind", "research", "--request-id", "req_one", "Look into one"]) == 0
    first = capsys.readouterr().out.strip()
    assert run(["start", "--kind", "research", "--request-id", "req_two", "Look into two"]) == 0
    second = capsys.readouterr().out.strip()

    assert first != second
    assert len(spawns) == 2
    assert len(list_jobs(onyx_env.cfg)) == 2


def test_concurrent_starts_with_same_request_id_create_one_job(onyx_env):
    barrier = threading.Barrier(2)
    results: list[tuple[str, bool]] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait()
            job, created = create_job_idempotent(
                onyx_env.cfg,
                request_id="req_race",
                kind="research",
                task="Race the create",
                repo=None,
            )
            results.append((job.id, created))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert len(list_jobs(onyx_env.cfg)) == 1
    assert len({job_id for job_id, _ in results}) == 1
    assert sorted(created for _, created in results) == [False, True]


def test_concurrent_cli_starts_spawn_once(onyx_env, monkeypatch):
    spawns: list[int] = []
    monkeypatch.setattr("onyx_pipeline.cli.spawn_runner", lambda cfg: spawns.append(1))
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []
    argv = ["start", "--kind", "research", "--request-id", "req_cli_race", "Race the CLI start"]

    def worker() -> None:
        try:
            barrier.wait()
            assert run(argv) == 0
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert len(spawns) == 1
    jobs = list_jobs(onyx_env.cfg)
    assert len(jobs) == 1
    assert jobs[0].request_id == "req_cli_race"
