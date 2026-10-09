"""onyx-job command-line interface."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from onyx_pipeline.config import Config, load_config
from onyx_pipeline.jobs import (
    create_job,
    create_job_idempotent,
    find_job_by_request_id,
    list_jobs,
    load_job,
)
from onyx_pipeline.stages import (
    answer_job,
    approve_job,
    cancel_job,
    find_duplicate_job,
    rerun_job,
    resolve_waiting_job,
    revise_job,
    run_loop,
    spawn_runner,
)
from onyx_pipeline.summary import render_summary


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="onyx-job")
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start", help="create a job and start the runner")
    start.add_argument("--kind", choices=("code", "research", "ops"), required=True)
    start.add_argument("--repo", default=None)
    start.add_argument(
        "--auto", action="store_true", help="skip the plan-approval gate for code jobs"
    )
    start.add_argument(
        "--auto-quote",
        default=None,
        help="David's exact words authorising --auto (required with --auto)",
    )
    start.add_argument(
        "--deep",
        action="store_true",
        help="investigate with Grok 4.6 high (slower, costs more). Default: decided by task triage",
    )
    start.add_argument(
        "--quick",
        action="store_true",
        help="investigate with Composer even if the task triage would pick Grok",
    )
    start.add_argument(
        "--allow-duplicate",
        action="store_true",
        help="allow a similar task in the duplicate window",
    )
    start.add_argument(
        "--request-id",
        default=None,
        help="caller-supplied id (req_...); a repeat start returns the original job id",
    )
    start.add_argument("task")

    status = sub.add_parser("status", help="show job state")
    status.add_argument("id", nargs="?")

    summary = sub.add_parser("summary", help="print summary.md")
    summary.add_argument("id")

    approve = sub.add_parser("approve", help="approve a needs-approval job")
    approve.add_argument("id", nargs="?")

    answer = sub.add_parser("answer", help="answer open questions and re-plan")
    answer.add_argument("args", nargs="+", metavar="arg")

    revise = sub.add_parser("revise", help="send plan feedback and re-plan")
    revise.add_argument("args", nargs="+", metavar="arg")

    cancel = sub.add_parser("cancel", help="cancel a job")
    cancel.add_argument("id")
    cancel.add_argument(
        "--confirm",
        action="store_true",
        help="only cancel when David asked",
    )

    rerun = sub.add_parser("rerun", help="run one stage of a finished job again (no rebuild)")
    rerun.add_argument("id")
    rerun.add_argument(
        "--stage",
        required=True,
        choices=("verify", "review", "fact-check"),
    )
    rerun.add_argument("--fix", action="store_true")

    sub.add_parser("list", help="list jobs")
    wait = sub.add_parser(
        "wait", help="block until a job finishes or needs David, then print its summary"
    )
    wait.add_argument("id")
    wait.add_argument("--poll", type=float, default=5.0, help=argparse.SUPPRESS)
    sub.add_parser("resume", help="restart the runner if a job is waiting and none is running")
    return parser


def _resolve_id_and_text(args: list[str], *, state: str) -> tuple[str | None, str]:
    if not args:
        raise ValueError("missing text")
    if len(args) == 1:
        job = resolve_waiting_job(load_config(), state)
        return job.id, args[0]
    found = _jobs_starting(load_config(), args[0])
    if len(found) > 1:
        raise ValueError(
            f"{args[0]} matches {len(found)} jobs, give the full id: {' '.join(found)}"
        )
    if not found:
        raise ValueError(f"unknown job {args[0]}. Run `onyx-job list` to see the job ids.")
    return found[0], " ".join(args[1:])


def _jobs_starting(cfg: Config, short: str) -> list[str]:
    """Ids of the jobs whose id is `short` or starts with it (a literal prefix, not a pattern)."""
    if not cfg.jobs_dir.exists():
        return []
    names = sorted(
        p.name
        for p in cfg.jobs_dir.iterdir()
        if p.name.startswith(short) and (p / "state.json").exists()
    )
    return [short] if short in names else names


def _status_line(job: object) -> str:
    from onyx_pipeline.jobs import Job

    assert isinstance(job, Job)
    extra = job.error or job.kind
    return f"{job.id} {job.display_state()} {extra}"


def run(argv: Sequence[str] | None = None) -> int:
    argv_list = list(argv) if argv is not None else sys.argv[1:]
    cfg = load_config()
    if argv_list[:1] == ["_run"]:
        run_loop(cfg)
        return 0
    args = _parser().parse_args(argv_list)

    short = getattr(args, "id", None)
    if short and args.command != "start" and not (cfg.jobs_dir / short).exists():
        # A job id may be given by its start: the date-time part is enough when only one job has it.
        found = _jobs_starting(cfg, short)
        if len(found) == 1:
            args.id = found[0]
        elif found:
            print(f"error: {short} matches {len(found)} jobs, give the full id:", file=sys.stderr)
            print("\n".join(found), file=sys.stderr)
            return 2
        else:
            hint = "Run `onyx-job list` to see the job ids."
            print(f"error: unknown job {short}. {hint}", file=sys.stderr)
            return 1

    if args.command == "start":
        if args.kind == "ops":
            bypass = [
                name
                for name, used in (
                    ("--repo", args.repo is not None),
                    ("--auto", args.auto),
                    ("--auto-quote", args.auto_quote is not None),
                    ("--deep", args.deep),
                    ("--quick", args.quick),
                )
                if used
            ]
            if bypass:
                print(
                    f"error: {' '.join(bypass)} not allowed with --kind ops",
                    file=sys.stderr,
                )
                return 2
        if args.kind == "code" and not args.repo:
            print("error: --repo is required for --kind code", file=sys.stderr)
            return 2
        if args.deep and args.quick:
            print("error: --deep and --quick cannot be used together", file=sys.stderr)
            return 2
        if args.auto and not (args.auto_quote and args.auto_quote.strip()):
            print(
                'error: start --auto requires --auto-quote "<David\'s exact words>"',
                file=sys.stderr,
            )
            return 2
        if args.request_id:
            existing = find_job_by_request_id(cfg, args.request_id)
            if existing is not None:
                # Repeat start: return the original job and start nothing.
                print(existing.id)
                return 0
        if not args.allow_duplicate:
            dup = find_duplicate_job(
                cfg,
                kind=args.kind,
                repo=args.repo,
                task=args.task,
                window_min=cfg.dup_window_min,
            )
            if dup is not None:
                from datetime import datetime

                age_min = 0
                if dup.stages:
                    try:
                        created = datetime.fromisoformat(str(dup.stages[0]["at"]))
                        if created.tzinfo is None:
                            created = created.astimezone()
                        age_min = int((datetime.now().astimezone() - created).total_seconds() / 60)
                    except ValueError:
                        pass
                print(
                    f"error: similar job {dup.id} is {dup.state} (started {age_min} min ago); "
                    "use --allow-duplicate to override",
                    file=sys.stderr,
                )
                return 2
        if args.request_id:
            job, created = create_job_idempotent(
                cfg,
                request_id=args.request_id,
                kind=args.kind,
                task=args.task,
                repo=args.repo,
                auto=args.auto,
                auto_quote=args.auto_quote.strip() if args.auto_quote else None,
                deep=True if args.deep else (False if args.quick else None),
            )
        else:
            job = create_job(
                cfg,
                kind=args.kind,
                task=args.task,
                repo=args.repo,
                auto=args.auto,
                auto_quote=args.auto_quote.strip() if args.auto_quote else None,
                deep=True if args.deep else (False if args.quick else None),
            )
            created = True
        if created:
            spawn_runner(cfg)
        print(job.id)
        return 0

    if args.command == "status":
        if args.id:
            try:
                job = load_job(cfg, args.id)
            except FileNotFoundError:
                print(f"error: unknown job {args.id}", file=sys.stderr)
                return 1
            print(_status_line(job))
            return 0
        for job in list_jobs(cfg):
            print(_status_line(job))
        return 0

    if args.command == "list":
        for job in list_jobs(cfg):
            print(_status_line(job))
        return 0

    if args.command == "summary":
        try:
            job = load_job(cfg, args.id)
        except FileNotFoundError:
            print(f"error: unknown job {args.id}", file=sys.stderr)
            return 1
        from onyx_pipeline.jobs import job_dir

        path = job_dir(cfg, job.id) / "summary.md"
        if path.exists():
            sys.stdout.write(path.read_text())
        else:
            sys.stdout.write(render_summary(cfg, job))
        return 0

    if args.command == "approve":
        job_id = args.id
        if not job_id:
            try:
                job_id = resolve_waiting_job(cfg, "needs-approval").id
            except ValueError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
        try:
            approve_job(cfg, job_id)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        spawn_runner(cfg)
        return 0

    if args.command == "answer":
        try:
            job_id, text = _resolve_id_and_text(args.args, state="needs-answer")
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        answer_job(cfg, job_id, text)
        spawn_runner(cfg)
        return 0

    if args.command == "revise":
        try:
            job_id, text = _resolve_id_and_text(args.args, state="needs-approval")
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        try:
            revise_job(cfg, job_id, text)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        spawn_runner(cfg)
        return 0

    if args.command == "wait":
        from onyx_pipeline.wait import wait_for_job

        return wait_for_job(cfg, args.id, poll=args.poll)

    if args.command == "resume":
        from onyx_pipeline.jobs import lock_held, next_runnable

        job = next_runnable(cfg)
        if job is None:
            print("nothing to resume")
        elif lock_held(cfg):
            print(f"runner already active ({job.id} {job.state})")
        else:
            spawn_runner(cfg)
            print(f"resumed {job.id} at {job.state}")
        return 0

    if args.command == "cancel":
        if not args.confirm:
            print("error: cancel needs --confirm; only cancel when David asked", file=sys.stderr)
            return 2
        cancel_job(cfg, args.id)
        return 0

    if args.command == "rerun":
        try:
            load_job(cfg, args.id)
        except FileNotFoundError:
            print(f"error: unknown job {args.id}", file=sys.stderr)
            return 1
        try:
            job = rerun_job(cfg, args.id, args.stage, fix=args.fix)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        spawn_runner(cfg)
        print(job.id)
        return 0

    return 2


def main(argv: Sequence[str] | None = None) -> None:
    raise SystemExit(run(argv))


if __name__ == "__main__":
    main()
