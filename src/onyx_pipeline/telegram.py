"""Telegram Bot API helpers (token from openclaw config)."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from onyx_pipeline.config import Config

_HTTP_TIMEOUT = 5
_MAX_MESSAGE_LEN = 4000
_MAX_CAPTION_LEN = 1024
_UPLOAD_TIMEOUT = 30

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ApiResult:
    ok: bool
    status: int | None
    description: str
    result: dict[str, Any] | None = None


def task_title(task: str) -> str:
    line = task.strip().splitlines()[0] if task.strip() else task
    if len(line) <= 60:
        return line
    return line[:59] + "…"


def read_bot_token(cfg: Config) -> str | None:
    try:
        raw = json.loads(cfg.openclaw_config.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    channels = raw.get("channels")
    if not isinstance(channels, dict):
        return None
    telegram = channels.get("telegram")
    if not isinstance(telegram, dict):
        return None
    accounts = telegram.get("accounts")
    if not isinstance(accounts, dict):
        return None
    account = accounts.get(cfg.notify_account)
    if not isinstance(account, dict):
        return None
    token = account.get("botToken")
    if not isinstance(token, str) or not token:
        return None
    return token


def _api_url(cfg: Config, token: str, method: str) -> str:
    base = cfg.progress_api.rstrip("/")
    return f"{base}/bot{token}/{method}"


def api_post(cfg: Config, token: str, method: str, body: dict[str, object]) -> ApiResult:
    url = _api_url(cfg, token, method)
    payload = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        status = exc.code
        description = ""
        try:
            err_body = json.loads(exc.read().decode())
            if isinstance(err_body, dict):
                desc = err_body.get("description")
                if isinstance(desc, str):
                    description = desc
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            pass
        _log.debug("Telegram HTTP %s: %s", status, type(exc).__name__)
        return ApiResult(ok=False, status=status, description=description)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        _log.debug("Telegram request failed: %s", type(exc).__name__)
        return ApiResult(ok=False, status=None, description="")
    try:
        parsed = json.loads(raw.decode())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return ApiResult(ok=False, status=status, description="")
    if not isinstance(parsed, dict) or not parsed.get("ok"):
        desc = parsed.get("description") if isinstance(parsed, dict) else ""
        if not isinstance(desc, str):
            desc = ""
        return ApiResult(ok=False, status=status, description=desc)
    result = parsed.get("result")
    if isinstance(result, dict):
        return ApiResult(ok=True, status=status, description="", result=result)
    if result is True:
        return ApiResult(ok=True, status=status, description="", result={})
    return ApiResult(ok=False, status=status, description="")


def _split_text(text: str, max_len: int = _MAX_MESSAGE_LEN) -> list[str]:
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
    rest = text
    while rest:
        if len(rest) <= max_len:
            chunks.append(rest)
            break
        piece = rest[:max_len]
        split_at = piece.rfind("\n")
        if split_at <= 0:
            split_at = max_len
        chunks.append(rest[:split_at].rstrip("\n") or rest[:split_at])
        rest = rest[split_at:].lstrip("\n")
    return chunks


def send_message(cfg: Config, text: str, *, silent: bool) -> int | None:
    token = read_bot_token(cfg)
    if not token:
        return None
    last_id: int | None = None
    for chunk in _split_text(text):
        body: dict[str, object] = {
            "chat_id": cfg.progress_chat_id,
            "text": chunk,
            "disable_notification": silent,
        }
        try:
            res = api_post(cfg, token, "sendMessage", body)
        except Exception as exc:  # noqa: BLE001
            _log.debug("send_message failed: %s", type(exc).__name__)
            return None
        if not res.ok or res.result is None:
            if res.status is not None:
                _log.debug("send_message HTTP %s", res.status)
            return None
        mid = res.result.get("message_id")
        if isinstance(mid, int):
            last_id = mid
        else:
            return None
    return last_id


def send_document(cfg: Config, path: Path, caption: str, *, silent: bool) -> int | None:
    """Upload a file as a Telegram document; returns the message_id or None."""
    token = read_bot_token(cfg)
    if not token:
        return None
    boundary = uuid.uuid4().hex
    fields = {
        "chat_id": cfg.progress_chat_id,
        "caption": caption[:_MAX_CAPTION_LEN],
        "disable_notification": "true" if silent else "false",
    }
    body = b"".join(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
        for k, v in fields.items()
    )
    mime = "text/markdown" if path.suffix == ".md" else "application/octet-stream"
    body += (
        f'--{boundary}\r\nContent-Disposition: form-data; name="document"; '
        f'filename="{path.name}"\r\nContent-Type: {mime}\r\n\r\n'
    ).encode()
    body += path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        _api_url(cfg, token, "sendDocument"),
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_UPLOAD_TIMEOUT) as resp:
            parsed = json.loads(resp.read().decode())
    except (OSError, TimeoutError, ValueError) as exc:
        _log.debug("send_document failed: %s", type(exc).__name__)
        return None
    result = parsed.get("result") if isinstance(parsed, dict) else None
    mid = result.get("message_id") if isinstance(result, dict) else None
    return mid if isinstance(mid, int) else None


def edit_message(cfg: Config, message_id: int, text: str) -> bool:
    token = read_bot_token(cfg)
    if not token:
        return False
    if len(text) > _MAX_MESSAGE_LEN:
        return False
    body = {
        "chat_id": cfg.progress_chat_id,
        "message_id": message_id,
        "text": text,
    }
    try:
        res = api_post(cfg, token, "editMessageText", body)
    except Exception as exc:  # noqa: BLE001
        _log.debug("edit_message failed: %s", type(exc).__name__)
        return False
    if res.ok:
        return True
    if res.status is not None:
        _log.debug("edit_message HTTP %s", res.status)
    return False


def edit_message_result(cfg: Config, message_id: int, text: str) -> ApiResult:
    """Like edit_message but returns the full API result (for progress retries)."""
    token = read_bot_token(cfg)
    if not token:
        return ApiResult(ok=False, status=None, description="")
    body = {
        "chat_id": cfg.progress_chat_id,
        "message_id": message_id,
        "text": text,
    }
    try:
        return api_post(cfg, token, "editMessageText", body)
    except Exception as exc:  # noqa: BLE001
        _log.debug("edit_message failed: %s", type(exc).__name__)
        return ApiResult(ok=False, status=None, description="")


def delete_message(cfg: Config, message_id: int) -> bool:
    token = read_bot_token(cfg)
    if not token:
        return False
    body = {
        "chat_id": cfg.progress_chat_id,
        "message_id": message_id,
    }
    try:
        res = api_post(cfg, token, "deleteMessage", body)
    except Exception as exc:  # noqa: BLE001
        _log.debug("delete_message failed: %s", type(exc).__name__)
        return False
    if res.ok:
        return True
    if res.status is not None:
        _log.debug("delete_message HTTP %s", res.status)
    return False


def delete_message_result(cfg: Config, message_id: int) -> ApiResult:
    token = read_bot_token(cfg)
    if not token:
        return ApiResult(ok=False, status=None, description="")
    body = {
        "chat_id": cfg.progress_chat_id,
        "message_id": message_id,
    }
    try:
        return api_post(cfg, token, "deleteMessage", body)
    except Exception as exc:  # noqa: BLE001
        _log.debug("delete_message failed: %s", type(exc).__name__)
        return ApiResult(ok=False, status=None, description="")


def send_message_result(cfg: Config, text: str, *, silent: bool) -> tuple[ApiResult, int | None]:
    """Single-chunk send returning API result and message_id (for progress)."""
    token = read_bot_token(cfg)
    if not token:
        return ApiResult(ok=False, status=None, description=""), None
    body: dict[str, object] = {
        "chat_id": cfg.progress_chat_id,
        "text": text,
        "disable_notification": silent,
    }
    try:
        res = api_post(cfg, token, "sendMessage", body)
    except Exception as exc:  # noqa: BLE001
        _log.debug("send_message failed: %s", type(exc).__name__)
        return ApiResult(ok=False, status=None, description=""), None
    if not res.ok or res.result is None:
        return res, None
    mid = res.result.get("message_id")
    if isinstance(mid, int):
        return res, mid
    return res, None
