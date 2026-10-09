"""Environment-backed configuration for onyx-job."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

CONFIG_FILE = Path("~/.config/onyx-job/config.env").expanduser()


def _file_values() -> dict[str, str]:
    """KEY=VALUE lines from the optional config file. Environment variables win."""
    path = Path(os.environ.get("ONYX_CONFIG_FILE", str(CONFIG_FILE))).expanduser()
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _env(key: str, default: str) -> str:
    value = os.environ.get(key)
    if value is not None:
        return value
    return _file_values().get(key, default)


def parse_quiet_hours(value: str) -> tuple[int, int]:
    start_s, end_s = value.split("-", 1)
    return int(start_s), int(end_s)


def _parse_roots(value: str) -> tuple[Path, ...]:
    parts = [p for p in value.split(":") if p]
    return tuple(Path(p).expanduser() for p in parts)


@dataclass(frozen=True)
class Config:
    jobs_dir: Path
    research_dir: Path
    allowed_roots: tuple[Path, ...]
    cursor_bin: str
    claude_bin: str
    codex_bin: str
    openclaw_bin: str
    notify_session: str
    notify_account: str
    notify_model: str
    notify_to: str
    worker_timeout: int
    verify_timeout: int
    quiet_hours: tuple[int, int]
    no_notify: bool
    no_deliver: bool
    dup_window_min: int
    progress_api: str
    progress_chat_id: str
    openclaw_config: Path
    no_progress: bool
    limit_cursor: int
    max_parts: int
    auto_approve: bool
    playbooks_dir: Path
    think_timeout: int
    think_daily_limit: int
    think_max_repairs: int
    agents_md: Path
    think_backend: str
    think_model: str
    opencode_bin: str
    build_backend: str
    build_model: str
    idle_limit: int
    watch_poll: float
    research_timeout: int
    deep_threshold: int


def load_config() -> Config:
    notify_session = _env("ONYX_NOTIFY_SESSION", "agent:onyx:telegram:direct:0")
    return Config(
        jobs_dir=Path(_env("ONYX_JOBS_DIR", "~/.openclaw/workspace-onyx/jobs")).expanduser(),
        research_dir=Path(
            _env("ONYX_RESEARCH_DIR", "~/.openclaw/workspace-onyx/research")
        ).expanduser(),
        allowed_roots=_parse_roots(_env("ONYX_ALLOWED_ROOTS", "~/Projects:/mnt/more-extra/Repos")),
        cursor_bin=_env("ONYX_CURSOR_BIN", "cursor-agent"),
        claude_bin=_env("ONYX_CLAUDE_BIN", "claude"),
        codex_bin=_env("ONYX_CODEX_BIN", "codex"),
        openclaw_bin=_env("ONYX_OPENCLAW_BIN", "openclaw"),
        notify_session=notify_session,
        notify_account=_env("ONYX_NOTIFY_ACCOUNT", "onyx"),
        notify_model=_env("ONYX_NOTIFY_MODEL", ""),
        notify_to=_env("ONYX_NOTIFY_TO", ""),
        worker_timeout=int(_env("ONYX_WORKER_TIMEOUT", "2700")),
        verify_timeout=int(_env("ONYX_VERIFY_TIMEOUT", "1200")),
        quiet_hours=parse_quiet_hours(_env("ONYX_QUIET_HOURS", "23-7")),
        no_notify=_env("ONYX_NO_NOTIFY", "") != "",
        no_deliver=_env("ONYX_NO_DELIVER", "") != "" or _env("ONYX_NO_NOTIFY", "") != "",
        dup_window_min=int(_env("ONYX_DUP_WINDOW_MIN", "30")),
        progress_api=_env("ONYX_PROGRESS_API", "https://api.telegram.org"),
        progress_chat_id=_env(
            "ONYX_PROGRESS_CHAT_ID",
            _env("ONYX_NOTIFY_TO", "") or notify_session.rsplit(":", 1)[-1],
        ),
        openclaw_config=Path(
            _env("ONYX_OPENCLAW_CONFIG", "~/.openclaw/openclaw.json")
        ).expanduser(),
        no_progress=_env("ONYX_NO_PROGRESS", "") != "" or _env("ONYX_NO_NOTIFY", "") != "",
        limit_cursor=int(_env("ONYX_LIMIT_CURSOR", "3")),
        max_parts=int(_env("ONYX_MAX_PARTS", "3")),
        auto_approve=_env("ONYX_AUTO_APPROVE", "") != "",
        playbooks_dir=Path(
            _env("ONYX_PLAYBOOKS_DIR", "~/.openclaw/workspace-onyx/playbooks")
        ).expanduser(),
        think_timeout=int(_env("ONYX_THINK_TIMEOUT", "360")),
        think_daily_limit=int(_env("ONYX_THINK_DAILY_LIMIT", "8")),
        think_max_repairs=int(_env("ONYX_THINK_MAX_REPAIRS", "2")),
        agents_md=Path(_env("ONYX_AGENTS_MD", "~/.openclaw/workspace-onyx/AGENTS.md")).expanduser(),
        think_backend=_env("ONYX_THINK_BACKEND", "cursor"),
        think_model=_env("ONYX_THINK_MODEL", "opencode-go/mimo-v2.6-pro"),
        opencode_bin=str(Path(_env("ONYX_OPENCODE_BIN", "~/.opencode/bin/opencode")).expanduser()),
        build_backend=_env("ONYX_BUILD_BACKEND", "opencode"),
        build_model=_env("ONYX_BUILD_MODEL", "opencode-go/deepseek-v4.1-flash"),
        idle_limit=int(_env("ONYX_IDLE_LIMIT", "600")),
        watch_poll=float(_env("ONYX_WATCH_POLL", "20")),
        research_timeout=int(_env("ONYX_RESEARCH_TIMEOUT", "1200")),
        deep_threshold=int(_env("ONYX_DEEP_THRESHOLD", "4")),
    )
