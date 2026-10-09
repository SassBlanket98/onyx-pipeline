"""Live Telegram message edited on each job stage change."""

from __future__ import annotations

import json
import urllib.error
from datetime import datetime
from pathlib import Path

from onyx_pipeline.config import Config
from onyx_pipeline.jobs import SAVE_LOCK, Job, job_dir, save_job
from onyx_pipeline.telegram import (
    delete_message_result,
    edit_message_result,
    read_bot_token,
    send_message_result,
    task_title,
)
from onyx_pipeline.wait import STOP_STATES
from onyx_pipeline.workers import builder_label

_PROGRESS_FILE = "progress.json"
_LOG_FILE = "progress.log"

# Ops stages shown as ticks, in the order they happen; needs-approval reads as
# the wait for David's yes.
OPS_STAGE_LABELS: tuple[tuple[str, str], ...] = (
    ("briefing", "briefing"),
    ("needs-approval", "waiting for approval"),
    ("operating", "operating"),
    ("verifying", "verifying"),
)


def update_progress(cfg: Config, job: Job) -> None:
    if cfg.no_progress:
        return
    directory = job_dir(cfg, job.id)
    try:
        _update_progress(cfg, job, directory)
    except Exception as exc:
        _log_error(directory, exc, None)


def _update_progress(cfg: Config, job: Job, directory: Path) -> None:
    token = read_bot_token(cfg)
    if not token:
        return

    progress_path = directory / _PROGRESS_FILE
    message_id = _load_message_id(progress_path)

    if job.state in STOP_STATES:
        if message_id is None:
            return
        res = delete_message_result(cfg, message_id)
        if not res.ok and "message to delete not found" not in res.description:
            if res.status is not None and res.status >= 400:
                _log_error(directory, None, res.status)
            return
        progress_path.unlink(missing_ok=True)
        return

    text = _format_text(job)
    if message_id is None:
        res, new_id = send_message_result(cfg, text, silent=True)
        if not res.ok:
            if res.status is not None and res.status >= 400:
                _log_error(directory, None, res.status)
            else:
                _log_error(directory, urllib.error.URLError("telegram unreachable"), None)
            return
        if new_id is not None:
            _save_message_id(progress_path, new_id)
        return

    res = edit_message_result(cfg, message_id, text)
    if res.status is None:
        return
    if res.status == 400:
        desc = res.description or ""
        if "message is not modified" in desc:
            return
        if "message to edit not found" in desc:
            send_res, new_id = send_message_result(cfg, text, silent=True)
            if not send_res.ok:
                if send_res.status is not None and send_res.status >= 400:
                    _log_error(directory, None, send_res.status)
                else:
                    _log_error(directory, urllib.error.URLError("telegram unreachable"), None)
                return
            if new_id is not None:
                _save_message_id(progress_path, new_id)
            return
    if res.status >= 400:
        _log_error(directory, None, res.status)


def start_checklist(job: Job, planned: list[str]) -> None:
    existing = job.flags.get("checklist")
    if isinstance(existing, dict) and isinstance(existing.get("files"), dict):
        files = dict(existing["files"])
        for path in planned:
            files.setdefault(path, False)
        job.flags["checklist"] = {
            "files": files,
            "extra": list(existing.get("extra") or []),
            "at": existing.get("at") or "",
        }
        return
    job.flags["checklist"] = {
        "files": {path: False for path in planned},
        "extra": [],
        "at": "",
    }


def _checklist_ignore(path: str) -> bool:
    if path.startswith(".onyx/") or path.endswith(".pyc"):
        return True
    if path.startswith("__pycache__/") or "/__pycache__/" in path:
        return True
    if path.startswith("node_modules/") or "/node_modules/" in path:
        return True
    return False


def tick_checklist(cfg: Config, job: Job, changed: list[str]) -> None:
    try:
        with SAVE_LOCK:
            raw = job.flags.get("checklist")
            if not isinstance(raw, dict):
                return
            files = raw.get("files")
            if not isinstance(files, dict):
                return
            extra = list(raw.get("extra") or [])
            extra_set = set(extra)
            touched = False
            for path in changed:
                if _checklist_ignore(path):
                    continue
                if path in files:
                    if not files[path]:
                        files[path] = True
                        touched = True
                elif path not in extra_set:
                    extra.append(path)
                    extra_set.add(path)
                    touched = True
            if not touched:
                return
            raw["extra"] = extra
            raw["at"] = datetime.now().astimezone().strftime("%H:%M")
            save_job(cfg, job)
        update_progress(cfg, job)
    except Exception:
        return


def _ops_stage_lines(job: Job) -> list[str]:
    """Tick the ops stages the job has reached, in order."""
    reached = {item.get("state") for item in job.stages}
    reached.add(job.state)
    return [f"{'✅' if state in reached else '⬜'} {label}" for state, label in OPS_STAGE_LABELS]


def _format_text(job: Job) -> str:
    title = task_title(job.task)
    stage = job.state
    if job.stages:
        at = job.stages[-1].get("at", "")
        try:
            hhmm = datetime.fromisoformat(at).astimezone().strftime("%H:%M")
        except (TypeError, ValueError):
            hhmm = "??:??"
    else:
        hhmm = "??:??"
    lines = [f"⚙️ {title}", f"{stage} since {hhmm}"]
    if job.kind == "ops":
        lines.extend(_ops_stage_lines(job))
    if job.fix_rounds_used > 0:
        lines.append(f"fix round {job.fix_rounds_used}")
    if job.state in {"building", "fixing"}:
        raw = job.flags.get("checklist")
        if isinstance(raw, dict):
            files = raw.get("files")
            extra = raw.get("extra") or []
            planned = list(files.keys()) if isinstance(files, dict) else []
            if planned or extra:
                if job.build_model:
                    lines[1] = f"{stage} since {hhmm} ({builder_label(job.build_model)})"
                show = planned[:12]
                done = sum(1 for p in planned if files.get(p)) if isinstance(files, dict) else 0
                for path in show:
                    mark = "✅" if isinstance(files, dict) and files.get(path) else "⬜"
                    lines.append(f"{mark} {path}")
                if len(planned) > 12:
                    lines.append(f"… and {len(planned) - 12} more ({done} done)")
                if extra:
                    n = len(extra) if isinstance(extra, list) else 0
                    lines.append(f"+{n} other file(s) touched")
                at_tick = raw.get("at")
                if at_tick:
                    lines.append(f"last change {at_tick}")
    return "\n".join(lines)


def _load_message_id(path: Path) -> int | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    mid = data.get("message_id")
    if isinstance(mid, int):
        return mid
    return None


def _save_message_id(path: Path, message_id: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"message_id": message_id}) + "\n")
    tmp.replace(path)


def _log_error(directory: Path, exc: Exception | None, http_status: int | None) -> None:
    parts: list[str] = []
    if exc is not None:
        parts.append(type(exc).__name__)
    if http_status is not None:
        parts.append(f"HTTP {http_status}")
    if not parts:
        return
    line = " ".join(parts)
    try:
        path = directory / _LOG_FILE
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
