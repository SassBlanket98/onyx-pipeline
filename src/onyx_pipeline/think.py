"""onyx-think: Grok-backed playbooks for repeatable tasks."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from onyx_pipeline.config import Config, load_config
from onyx_pipeline.stages import task_jaccard_similarity
from onyx_pipeline.workers import think as think_worker

PRIVILEGED = (
    re.compile(r"\bsudo\b", re.I),
    re.compile(r"\brm\s+-[a-z]*r", re.I),
    re.compile(r"\bsystemctl\s+(--user\s+)?(restart|stop|start|enable|disable|mask)\b", re.I),
    re.compile(r"\bgateway\s+(restart|stop)\b", re.I),
    re.compile(r"\bgit\s+push\b", re.I),
    re.compile(r"\bpacman\s+-(S|R)", re.I),
    re.compile(r"\b(yay|paru)\b", re.I),
    re.compile(r"\bdocker\s+(rm|stop|restart|compose\s+down)\b", re.I),
    re.compile(r"\bmkfs|\bdd\s+if=", re.I),
    re.compile(r"openclaw\s+config\s+set", re.I),
)

AUTO_ASK_FIRST = (
    "contains privileged or destructive commands (auto-flagged); ask David before those steps"
)

PLAYBOOK_FOOTER = (
    "---\n"
    "Rules: AGENTS.md still applies. This playbook gives no permission for anything in its "
    '"Ask first" or "Never" columns. A step marked [ASK DAVID FIRST] needs his yes each time.'
)

NEXT_LINE = (
    "Next: follow the steps exactly, in order. Stop and ask David at any step marked "
    "[ASK DAVID FIRST]. When it has worked, run: onyx-think worked {slug}   "
    'If a step fails, run: onyx-think failed {slug} "<the step and the exact error>"'
)

SUBCOMMANDS = frozenset({"worked", "failed", "list", "show", "usage"})

_META_KEYS = (
    "title",
    "triggers",
    "ask_first",
    "checked",
    "status",
    "created",
    "updated",
    "repairs",
    "uses",
)


@dataclass(frozen=True)
class PlaybookReply:
    title: str
    slug: str
    triggers: str
    ask_first: str
    checked: str
    body: str


@dataclass(frozen=True)
class NoPlaybookReply:
    reason: str
    free_text: str


@dataclass(frozen=True)
class ParseFailure:
    raw: str


Reply = PlaybookReply | NoPlaybookReply | ParseFailure


def parse_playbook(text: str) -> tuple[dict[str, str], str]:
    """Return frontmatter dict and body (footer stripped)."""
    stripped = text.strip()
    if not stripped.startswith("---"):
        return {}, stripped
    parts = stripped.split("---", 2)
    if len(parts) < 3:
        return {}, stripped
    fm_block = parts[1].strip()
    rest = parts[2].lstrip("\n")
    meta: dict[str, str] = {}
    for line in fm_block.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip()
    body = rest
    footer_idx = body.find("\n---\nRules:")
    if footer_idx >= 0:
        body = body[:footer_idx]
    return meta, body.rstrip("\n")


def render_playbook(meta: dict[str, str], body: str) -> str:
    lines = ["---"]
    for key in _META_KEYS:
        if key in meta:
            lines.append(f"{key}: {meta[key]}")
    lines.append("---")
    lines.append("")
    lines.append(body.rstrip())
    lines.append("")
    lines.append(PLAYBOOK_FOOTER)
    return "\n".join(lines) + "\n"


def _strip_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```") and t.endswith("```"):
        inner = t[3:-3].strip()
        if inner.lower().startswith("markdown"):
            inner = inner[8:].lstrip("\n")
        return inner
    return text


def _find_reply_start(text: str) -> int:
    for i, line in enumerate(text.splitlines()):
        stripped = line.strip()
        if stripped == "PLAYBOOK" or stripped.startswith("NO-PLAYBOOK:"):
            return i
    return -1


def _sanitize_slug(raw: str, title: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", raw.lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    if not slug:
        slug = re.sub(r"[^a-z0-9-]+", "-", title.lower())
        slug = re.sub(r"-+", "-", slug).strip("-")
    return slug[:60].rstrip("-")


def _derive_slug_from_title(title: str) -> str:
    return _sanitize_slug("", title)


def _normalize_checked(value: str) -> str:
    v = value.strip().lower()
    if v in ("yes", "partly", "no"):
        return v
    return "no"


ASK_MARK = "[ASK DAVID FIRST]"


def _is_privileged(line: str) -> bool:
    return any(pat.search(line) for pat in PRIVILEGED)


def mark_privileged_steps(body: str) -> tuple[str, bool]:
    """Put the ask-first mark on every privileged command the thinker left unmarked.

    Onyx stops only at marked steps, so a flag in the frontmatter alone isn't enough.
    A command inside a code block gets the mark on a line above the block, so the
    command itself stays copyable. Returns (body, whether anything was flagged).
    """
    lines = body.splitlines()
    out: list[str] = []
    fence_start: int | None = None
    flagged = False
    for line in lines:
        if line.strip().startswith("```"):
            fence_start = len(out) if fence_start is None else None
            out.append(line)
            continue
        if _is_privileged(line) and ASK_MARK not in line:
            at = fence_start if fence_start is not None else len(out)
            nearby = out[max(0, at - 3) : at]
            if not any(ASK_MARK in prev for prev in nearby):
                out.insert(at, f"{ASK_MARK} The next command needs David's yes before you run it.")
                if fence_start is not None:
                    fence_start += 1
            flagged = True
        out.append(line)
    return "\n".join(out), flagged


def parse_reply(text: str) -> Reply:
    text = _strip_fence(text)
    start = _find_reply_start(text)
    if start < 0:
        return ParseFailure(raw=text)
    all_lines = text.splitlines()
    lines = all_lines[start:]
    # A fenced reply after a preamble: drop the fence that closes it.
    if start > 0 and all_lines[start - 1].strip().startswith("```"):
        while lines and not lines[-1].strip():
            lines.pop()
        if lines and lines[-1].strip() == "```":
            lines.pop()
    first = lines[0].strip()
    if first.startswith("NO-PLAYBOOK:"):
        reason = first[len("NO-PLAYBOOK:") :].strip()
        free = "\n".join(lines[1:]).strip()
        return NoPlaybookReply(reason=reason, free_text=free)

    if first != "PLAYBOOK":
        return ParseFailure(raw=text)

    header: dict[str, str] = {}
    body_lines: list[str] = []
    in_body = False
    for line in lines[1:]:
        if not in_body:
            if line.strip() == "---":
                in_body = True
                continue
            if ":" in line:
                key, value = line.split(":", 1)
                header[key.strip().lower()] = value.strip()
            continue
        body_lines.append(line)
    body = "\n".join(body_lines).strip()
    title = header.get("title", "").strip()
    triggers = header.get("triggers", "").strip()
    if not title or not triggers or not body:
        return ParseFailure(raw=text)
    slug_raw = header.get("slug", "").strip()
    slug = _sanitize_slug(slug_raw, title) if slug_raw else _derive_slug_from_title(title)
    if not slug:
        return ParseFailure(raw=text)
    ask_first = header.get("ask-first", header.get("ask_first", "none")).strip() or "none"
    checked = _normalize_checked(header.get("checked", "no"))
    body, flagged = mark_privileged_steps(body)
    if flagged and ask_first.strip().lower() == "none":
        ask_first = AUTO_ASK_FIRST
    return PlaybookReply(
        title=title,
        slug=slug,
        triggers=triggers,
        ask_first=ask_first,
        checked=checked,
        body=body,
    )


def _usage_path(cfg: Config) -> Path:
    return cfg.playbooks_dir / ".usage.jsonl"


def _think_log_path(cfg: Config) -> Path:
    return cfg.playbooks_dir / ".think.log"


def _today_local() -> date:
    return datetime.now().astimezone().date()


def _parse_usage_ts(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def _usage_today(cfg: Config) -> int:
    path = _usage_path(cfg)
    if not path.is_file():
        return 0
    today = _today_local()
    count = 0
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = _parse_usage_ts(str(row.get("ts", "")))
        if ts is not None and ts.astimezone().date() == today:
            count += 1
    return count


def _log_usage(cfg: Config, *, kind: str, slug: str | None, ok: bool) -> None:
    path = _usage_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "kind": kind,
        "slug": slug,
        "ok": ok,
    }
    with path.open("a") as fh:
        fh.write(json.dumps(row) + "\n")


def _limit_message(n: int, limit: int) -> str:
    return (
        f"[onyx-think limit] The thinker has been used {n} times today (limit {limit}). "
        "No playbook was written. Tell David and ask whether to carry on without one."
    )


def _playbook_files(cfg: Config) -> list[Path]:
    root = cfg.playbooks_dir
    if not root.is_dir():
        return []
    return sorted(p for p in root.glob("*.md") if p.is_file())


def _slug_from_path(path: Path) -> str:
    return path.stem


def _load_playbook(path: Path) -> tuple[dict[str, str], str]:
    return parse_playbook(path.read_text())


def _trigger_phrases(triggers: str) -> list[str]:
    return [p.strip() for p in triggers.split(";") if p.strip()]


def _best_similarity(task: str, meta: dict[str, str]) -> float:
    title = meta.get("title", "")
    phrases = [title] + _trigger_phrases(meta.get("triggers", ""))
    if not phrases:
        return 0.0
    return max(task_jaccard_similarity(task, p) for p in phrases)


def _find_existing(cfg: Config, task: str) -> Path | None:
    best_path: Path | None = None
    best_score = 0.0
    for path in _playbook_files(cfg):
        meta, _ = _load_playbook(path)
        status = meta.get("status", "")
        if status == "broken":
            continue
        score = _best_similarity(task, meta)
        if score > best_score:
            best_score = score
            best_path = path
    if best_score >= 0.6 and best_path is not None:
        return best_path
    return None


def _allocate_slug_path(cfg: Config, slug: str) -> tuple[str, Path]:
    base = cfg.playbooks_dir / f"{slug}.md"
    if not base.exists():
        return slug, base
    n = 2
    while True:
        candidate = f"{slug}-{n}"
        path = cfg.playbooks_dir / f"{candidate}.md"
        if not path.exists():
            return candidate, path
        n += 1


def _playbook_path(cfg: Config, slug: str) -> Path | None:
    """The playbook file for a slug, or None. A slug is a bare name, never a path."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug):
        return None
    path = cfg.playbooks_dir / f"{slug}.md"
    return path if path.is_file() else None


def _today_str() -> str:
    return _today_local().isoformat()


def _print_playbook_output(slug: str, text: str, meta: dict[str, str]) -> None:
    print(text, end="" if text.endswith("\n") else "\n")
    ask_first = meta.get("ask_first", "none")
    if ask_first.strip().lower() != "none":
        print(f"Ask first: {ask_first}")
    print(NEXT_LINE.format(slug=slug))


def _invoke_thinker(cfg: Config, prompt: str, *, kind: str, slug: str | None) -> tuple[int, str]:
    cfg.playbooks_dir.mkdir(parents=True, exist_ok=True)
    code, out = think_worker(cfg, _think_log_path(cfg), cfg.playbooks_dir, prompt)
    _log_usage(cfg, kind=kind, slug=slug, ok=code == 0 and bool(out.strip()))
    return code, out


def _save_unparsed(cfg: Config, raw: str) -> Path:
    hist = cfg.playbooks_dir / ".history"
    hist.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = hist / f"unparsed-{stamp}.txt"
    path.write_text(raw)
    return path


def _playbook_format_block() -> str:
    return (
        "PLAYBOOK\n"
        "TITLE: <one line>\n"
        "SLUG: <kebab-case, 2-6 words>\n"
        "TRIGGERS: <3 to 8 short phrases David might type for this task, separated by ;>\n"
        "ASK-FIRST: none | <what needs David's yes>\n"
        "CHECKED: yes | partly | no\n"
        "---\n"
        "<body>"
    )


def new_prompt(task: str, agents_md: Path) -> str:
    agents = str(agents_md)
    fmt = _playbook_format_block()
    return (
        "You are writing a playbook for Onyx, a 9B local model that runs on David's Arch Linux "
        "desktop with shell access as user david. Onyx will follow the steps literally and cannot "
        "work out missing details, so every step needs the exact command or tool call, with real "
        "paths and real flags.\n"
        "Work out the correct way to do the task on THIS machine. Check instead of assuming: you "
        "may run read-only commands (which, --help, --version, ls, cat, status and list commands, "
        "dry runs). You must not change anything: no writes, installs, restarts, sends, deletes, "
        "or config changes while thinking.\n"
        f'Read section 1 of "{agents}" for what Onyx may do freely, must ask first, and must '
        "never do. A playbook cannot override it. Any step in the ask-first column gets the prefix "
        "[ASK DAVID FIRST] and is named in ASK-FIRST:. Never include a step from the never column; "
        "say what David has to do himself instead.\n"
        "Never write a secret, token, or password into the playbook. Say where it is stored.\n"
        "Body sections, in this order, with these headings: ## When to use, ## Steps (numbered, "
        "one command per step in a code block, then one line saying what the output should look "
        "like), ## How to tell it worked, ## If it fails (the likely failures and what to do), "
        "## Not checked (anything you could not verify; write Nothing. if all was verified).\n"
        "CHECKED: yes only if every command was verified to exist with those flags; partly if "
        "some; no if none.\n"
        "Keep the body under 3000 characters.\n"
        "If the task is not a repeatable procedure (a one-off question, an opinion, chat), or you "
        "cannot find a correct way, reply NO-PLAYBOOK: <reason> and then the answer or what is "
        "missing.\n"
        "Output only the format below, nothing before or after.\n"
        f"{fmt}\n"
        f"Task: {task}"
    )


def repair_prompt(task_title: str, old_playbook_text: str, failure: str, agents_md: Path) -> str:
    base = new_prompt(task_title, agents_md)
    return (
        f"{base}\n"
        "The current playbook is below in full. Onyx followed it and it failed.\n"
        f"What happened: {failure}\n"
        "Find the real cause by checking on the machine, fix the playbook, keep the same SLUG, "
        "and add the failure to ## If it fails.\n"
        "---\n"
        f"{old_playbook_text}"
    )


def cmd_new(cfg: Config, task: str) -> int:
    if not task.strip():
        print('usage: onyx-think "<task>"', file=sys.stderr)
        return 2

    existing = _find_existing(cfg, task)
    if existing is not None:
        slug = _slug_from_path(existing)
        text = existing.read_text()
        meta, _ = _load_playbook(existing)
        print(f"[onyx-think existing {slug}]")
        _print_playbook_output(slug, text, meta)
        return 0

    limit = cfg.think_daily_limit
    n_today = _usage_today(cfg)
    if n_today >= limit:
        print(_limit_message(n_today, limit))
        return 3

    code, out = _invoke_thinker(cfg, new_prompt(task, cfg.agents_md), kind="new", slug=None)
    if code != 0 or not out.strip():
        print(
            f"[onyx-think failed] The thinker did not answer (exit {code}). "
            "Tell David; do not guess the steps."
        )
        return 1

    reply = parse_reply(out)
    if isinstance(reply, NoPlaybookReply):
        print(f"[onyx-think no-playbook] {reply.reason}")
        if reply.free_text:
            print(reply.free_text)
        return 0
    if isinstance(reply, ParseFailure):
        path = _save_unparsed(cfg, reply.raw)
        print(
            "[onyx-think failed] The thinker's answer was not in the expected format "
            f"(saved to {path}). Tell David; do not guess the steps."
        )
        return 1

    slug, path = _allocate_slug_path(cfg, reply.slug)
    today = _today_str()
    meta = {
        "title": reply.title,
        "triggers": reply.triggers,
        "ask_first": reply.ask_first,
        "checked": reply.checked,
        "status": "untested",
        "created": today,
        "updated": today,
        "repairs": "0",
        "uses": "0",
    }
    text = render_playbook(meta, reply.body)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    print(f"[onyx-think saved {slug}]")
    _print_playbook_output(slug, text, meta)
    return 0


def cmd_worked(cfg: Config, slug: str) -> int:
    path = _playbook_path(cfg, slug)
    if path is None:
        print(f"no playbook named {slug}", file=sys.stderr)
        return 2
    meta, body = _load_playbook(path)
    uses = int(meta.get("uses", "0") or "0") + 1
    meta["status"] = "works"
    meta["uses"] = str(uses)
    meta["updated"] = _today_str()
    path.write_text(render_playbook(meta, body))
    print(f"[onyx-think worked {slug}] noted (used {uses} times)")
    return 0


def cmd_failed(cfg: Config, slug: str, failure: str) -> int:
    if not failure.strip():
        print('usage: onyx-think failed <slug> "<what went wrong>"', file=sys.stderr)
        return 2
    path = _playbook_path(cfg, slug)
    if path is None:
        print(f"no playbook named {slug}", file=sys.stderr)
        return 2

    meta, body = _load_playbook(path)
    repairs = int(meta.get("repairs", "0") or "0")
    if repairs >= cfg.think_max_repairs:
        meta["status"] = "broken"
        path.write_text(render_playbook(meta, body))
        print(
            f"[onyx-think broken {slug}] This playbook has failed after {repairs} repairs. "
            "Stop. Tell David what failed and do not try other commands."
        )
        return 3

    limit = cfg.think_daily_limit
    n_today = _usage_today(cfg)
    if n_today >= limit:
        print(_limit_message(n_today, limit))
        return 3

    old_text = path.read_text()
    title = meta.get("title", slug)
    code, out = _invoke_thinker(
        cfg,
        repair_prompt(title, old_text, failure, cfg.agents_md),
        kind="repair",
        slug=slug,
    )

    def _mark_broken(msg: str) -> int:
        meta["status"] = "broken"
        path.write_text(render_playbook(meta, body))
        print(msg)
        return 1

    if code != 0 or not out.strip():
        return _mark_broken(
            f"[onyx-think broken {slug}] The thinker could not repair it. "
            "Stop. Tell David what failed."
        )

    reply = parse_reply(out)
    if isinstance(reply, NoPlaybookReply):
        suffix = f": {reply.reason}" if reply.reason else ""
        return _mark_broken(
            f"[onyx-think broken {slug}] The thinker could not repair it{suffix}. "
            "Stop. Tell David what failed."
        )
    if isinstance(reply, ParseFailure):
        return _mark_broken(
            f"[onyx-think broken {slug}] The thinker could not repair it. "
            "Stop. Tell David what failed."
        )

    hist = cfg.playbooks_dir / ".history"
    hist.mkdir(parents=True, exist_ok=True)
    hist_path = hist / f"{slug}-{repairs + 1}.md"
    hist_path.write_text(old_text)

    created = meta.get("created", _today_str())
    uses = meta.get("uses", "0")
    new_meta = {
        "title": reply.title,
        "triggers": reply.triggers,
        "ask_first": reply.ask_first,
        "checked": reply.checked,
        "status": "untested",
        "created": created,
        "updated": _today_str(),
        "repairs": str(repairs + 1),
        "uses": uses,
    }
    text = render_playbook(new_meta, reply.body)
    path.write_text(text)
    print(f"[onyx-think repaired {slug}]")
    _print_playbook_output(slug, text, new_meta)
    return 0


def cmd_list(cfg: Config) -> int:
    files = _playbook_files(cfg)
    if not files:
        print("no playbooks yet")
        return 0
    for path in files:
        meta, _ = _load_playbook(path)
        slug = _slug_from_path(path)
        status = meta.get("status", "")
        title = meta.get("title", "")
        print(f"{slug}  {status}  {title}")
    return 0


def cmd_show(cfg: Config, slug: str) -> int:
    path = _playbook_path(cfg, slug)
    if path is None:
        print(f"no playbook named {slug}", file=sys.stderr)
        return 2
    sys.stdout.write(path.read_text())
    return 0


def cmd_usage(cfg: Config) -> int:
    today = _usage_today(cfg)
    limit = cfg.think_daily_limit
    path = _usage_path(cfg)
    cutoff = datetime.now().astimezone() - timedelta(days=30)
    total_30 = 0
    new_30 = 0
    repair_30 = 0
    if path.is_file():
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = _parse_usage_ts(str(row.get("ts", "")))
            if ts is None or ts.astimezone() < cutoff:
                continue
            total_30 += 1
            kind = row.get("kind")
            if kind == "new":
                new_30 += 1
            elif kind == "repair":
                repair_30 += 1

    works = untested = broken = 0
    for p in _playbook_files(cfg):
        meta, _ = _load_playbook(p)
        st = meta.get("status", "")
        if st == "works":
            works += 1
        elif st == "untested":
            untested += 1
        elif st == "broken":
            broken += 1
    total_pb = len(_playbook_files(cfg))
    print(f"thinker calls today: {today} of {limit}")
    print(f"last 30 days: {total_30} ({new_30} new, {repair_30} repairs)")
    print(f"playbooks: {total_pb} ({works} works, {untested} untested, {broken} broken)")
    return 0


def main(argv: list[str] | None = None) -> int:
    raw = list(argv) if argv is not None else sys.argv[1:]
    cfg = load_config()

    if not raw:
        print('usage: onyx-think "<task>"', file=sys.stderr)
        return 2

    if raw[0] not in SUBCOMMANDS and not raw[0].startswith("-"):
        return cmd_new(cfg, " ".join(raw))

    parser = argparse.ArgumentParser(prog="onyx-think")
    sub = parser.add_subparsers(dest="command", required=True)

    worked = sub.add_parser("worked")
    worked.add_argument("slug")

    failed = sub.add_parser("failed")
    failed.add_argument("slug")
    failed.add_argument("failure", nargs=argparse.REMAINDER)

    sub.add_parser("list")

    show = sub.add_parser("show")
    show.add_argument("slug")

    sub.add_parser("usage")

    args = parser.parse_args(raw)

    if args.command == "worked":
        return cmd_worked(cfg, args.slug)
    if args.command == "failed":
        failure = " ".join(args.failure).strip()
        return cmd_failed(cfg, args.slug, failure)
    if args.command == "list":
        return cmd_list(cfg)
    if args.command == "show":
        return cmd_show(cfg, args.slug)
    if args.command == "usage":
        return cmd_usage(cfg)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
