"""Telegram stage progress message (send / edit / delete)."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from onyx_pipeline.config import load_config
from onyx_pipeline.jobs import create_job, job_dir, transition
from onyx_pipeline.progress import update_progress


class _Recorder:
    calls: list[tuple[str, dict]] = []
    fail_delete: bool = False


class _TelegramHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length).decode())
        _Recorder.calls.append((self.path, body))
        if _Recorder.fail_delete and self.path.endswith("/deleteMessage"):
            data = json.dumps({"ok": False, "description": "Too Many Requests"}).encode()
            self.send_response(429)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path.endswith("/sendMessage"):
            payload = {"ok": True, "result": {"message_id": 42}}
        else:
            payload = {"ok": True, "result": True}
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _start_server() -> tuple[HTTPServer, str]:
    _Recorder.calls = []
    server = HTTPServer(("127.0.0.1", 0), _TelegramHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, f"http://{host}:{port}"


def _write_openclaw(path: Path, token: str | None) -> None:
    account: dict[str, str] = {}
    if token is not None:
        account["botToken"] = token
    path.write_text(
        json.dumps(
            {
                "channels": {
                    "telegram": {
                        "accounts": {
                            "onyx": account,
                        }
                    }
                }
            }
        )
    )


def _progress_env(monkeypatch, tmp_path: Path, api_base: str) -> None:
    openclaw = tmp_path / "openclaw.json"
    _write_openclaw(openclaw, "fake-token-secret")
    monkeypatch.delenv("ONYX_NO_NOTIFY", raising=False)
    monkeypatch.delenv("ONYX_NO_PROGRESS", raising=False)
    monkeypatch.setenv("ONYX_PROGRESS_API", api_base)
    monkeypatch.setenv("ONYX_OPENCLAW_CONFIG", str(openclaw))


def test_send_then_edit(onyx_env, monkeypatch) -> None:
    server, api = _start_server()
    try:
        _progress_env(monkeypatch, onyx_env.tmp, api)
        cfg = load_config()
        job = create_job(cfg, kind="code", task="Build the thing\nmore lines", repo=None)
        transition(cfg, job, "investigating")
        assert len(_Recorder.calls) == 1
        path, body = _Recorder.calls[0]
        assert path.endswith("/sendMessage")
        assert body["disable_notification"] is True
        assert "investigating" in body["text"]
        assert job_dir(cfg, job.id).joinpath("progress.json").read_text()

        transition(cfg, job, "planning")
        assert len(_Recorder.calls) == 2
        path2, body2 = _Recorder.calls[1]
        assert path2.endswith("/editMessageText")
        assert body2["message_id"] == 42
        assert "planning" in body2["text"]
    finally:
        server.shutdown()


def test_done_deletes_message_and_json(onyx_env, monkeypatch) -> None:
    server, api = _start_server()
    try:
        _progress_env(monkeypatch, onyx_env.tmp, api)
        cfg = load_config()
        job = create_job(cfg, kind="code", task="Task", repo=None)
        transition(cfg, job, "building")
        progress = job_dir(cfg, job.id) / "progress.json"
        assert progress.is_file()

        transition(cfg, job, "done")
        assert len(_Recorder.calls) == 2
        assert _Recorder.calls[1][0].endswith("/deleteMessage")
        assert _Recorder.calls[1][1]["message_id"] == 42
        assert not progress.is_file()

        n_before = len(_Recorder.calls)
        update_progress(cfg, job)
        assert len(_Recorder.calls) == n_before
    finally:
        server.shutdown()


def test_fix_round_in_message(onyx_env, monkeypatch) -> None:
    server, api = _start_server()
    try:
        _progress_env(monkeypatch, onyx_env.tmp, api)
        cfg = load_config()
        job = create_job(cfg, kind="code", task="Task", repo=None)
        job.fix_rounds_used = 1
        update_progress(cfg, job)
        assert len(_Recorder.calls) == 1
        assert "fix round 1" in _Recorder.calls[0][1]["text"]
    finally:
        server.shutdown()


def test_server_down_logs_without_token(onyx_env, monkeypatch) -> None:
    server, api = _start_server()
    server.shutdown()
    _progress_env(monkeypatch, onyx_env.tmp, api)
    cfg = load_config()
    job = create_job(cfg, kind="code", task="Task", repo=None)
    update_progress(cfg, job)
    log_path = job_dir(cfg, job.id) / "progress.log"
    assert log_path.is_file()
    log_text = log_path.read_text()
    assert "fake-token-secret" not in log_text
    assert "URLError" in log_text or "Connection" in log_text or len(log_text) > 0


def test_no_progress_env_skips_requests(onyx_env, monkeypatch) -> None:
    server, api = _start_server()
    try:
        _progress_env(monkeypatch, onyx_env.tmp, api)
        monkeypatch.setenv("ONYX_NO_PROGRESS", "1")
        cfg = load_config()
        assert cfg.no_progress
        job = create_job(cfg, kind="code", task="Task", repo=None)
        update_progress(cfg, job)
        assert _Recorder.calls == []
    finally:
        server.shutdown()


def test_missing_bot_token_no_requests(onyx_env, monkeypatch) -> None:
    server, api = _start_server()
    try:
        openclaw = onyx_env.tmp / "openclaw-empty.json"
        _write_openclaw(openclaw, None)
        monkeypatch.delenv("ONYX_NO_NOTIFY", raising=False)
        monkeypatch.delenv("ONYX_NO_PROGRESS", raising=False)
        monkeypatch.setenv("ONYX_PROGRESS_API", api)
        monkeypatch.setenv("ONYX_OPENCLAW_CONFIG", str(openclaw))
        cfg = load_config()
        job = create_job(cfg, kind="code", task="Task", repo=None)
        update_progress(cfg, job)
        assert _Recorder.calls == []
    finally:
        server.shutdown()


def test_delete_failure_keeps_message_id(onyx_env, monkeypatch) -> None:
    server, api = _start_server()
    try:
        _progress_env(monkeypatch, onyx_env.tmp, api)
        cfg = load_config()
        job = create_job(cfg, kind="code", repo=None, task="Keep id on failed delete")
        transition(cfg, job, "building")
        progress_path = job_dir(cfg, job.id) / "progress.json"
        assert progress_path.exists()
        _Recorder.fail_delete = True
        transition(cfg, job, "done")
        assert progress_path.exists()
        _Recorder.fail_delete = False
        update_progress(cfg, job)
        assert not progress_path.exists()
    finally:
        _Recorder.fail_delete = False
        server.shutdown()


def test_chat_id_follows_notify_destination(monkeypatch) -> None:
    monkeypatch.delenv("ONYX_PROGRESS_CHAT_ID", raising=False)
    monkeypatch.setenv("ONYX_NOTIFY_SESSION", "agent:onyx:telegram:direct:555")
    assert load_config().progress_chat_id == "555"
    monkeypatch.setenv("ONYX_NOTIFY_TO", "777")
    assert load_config().progress_chat_id == "777"
    monkeypatch.setenv("ONYX_PROGRESS_CHAT_ID", "999")
    assert load_config().progress_chat_id == "999"


def test_delete_unreachable_keeps_message_id(onyx_env, monkeypatch) -> None:
    server, api = _start_server()
    try:
        _progress_env(monkeypatch, onyx_env.tmp, api)
        cfg = load_config()
        job = create_job(cfg, kind="code", repo=None, task="Keep id when Telegram is down")
        transition(cfg, job, "building")
        progress_path = job_dir(cfg, job.id) / "progress.json"
        assert progress_path.exists()
        server.shutdown()
        server.server_close()
        monkeypatch.setenv("ONYX_PROGRESS_API", "http://127.0.0.1:9")
        down_cfg = load_config()
        transition(down_cfg, job, "done")
        assert progress_path.exists()
    finally:
        server.shutdown()
