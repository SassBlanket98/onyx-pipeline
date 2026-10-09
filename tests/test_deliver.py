"""Telegram delivery of stop-state messages."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from onyx_pipeline.config import load_config
from onyx_pipeline.deliver import compose_stop_message, deliver_stop
from onyx_pipeline.jobs import create_job, job_dir, load_job, save_job, transition
from onyx_pipeline.stages import _stop
from onyx_pipeline.summary import write_summary
from onyx_pipeline.wait import wait_for_job


class _Recorder:
    calls: list[tuple[str, dict]] = []
    fail_send: bool = False
    fail_after_chunk: int | None = None


class _TelegramHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        if self.headers.get("Content-Type", "").startswith("multipart/"):
            body = {"_multipart": raw.decode("utf-8", "replace")}
        else:
            body = json.loads(raw.decode())
        _Recorder.calls.append((self.path, body))
        if _Recorder.fail_after_chunk is not None:
            send_n = sum(1 for p, _ in _Recorder.calls if p.endswith("/sendMessage"))
            if send_n > _Recorder.fail_after_chunk:
                data = json.dumps({"ok": False, "description": "error"}).encode()
                self.send_response(500)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
        if _Recorder.fail_send:
            data = json.dumps({"ok": False, "description": "error"}).encode()
            self.send_response(500)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        mid = 100 + len(_Recorder.calls)
        if self.path.endswith(("/sendMessage", "/sendDocument")):
            payload = {"ok": True, "result": {"message_id": mid}}
        else:
            payload = {"ok": True, "result": True}
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _start_server() -> tuple[HTTPServer, str]:
    _Recorder.calls = []
    _Recorder.fail_send = False
    _Recorder.fail_after_chunk = None
    server = HTTPServer(("127.0.0.1", 0), _TelegramHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, f"http://{host}:{port}"


def _write_openclaw(path: Path, token: str = "fake-token") -> None:
    path.write_text(
        json.dumps(
            {
                "channels": {
                    "telegram": {"accounts": {"onyx": {"botToken": token}}},
                }
            }
        )
    )


def _deliver_env(monkeypatch, tmp_path: Path, api_base: str) -> None:
    openclaw = tmp_path / "openclaw.json"
    _write_openclaw(openclaw)
    monkeypatch.delenv("ONYX_NO_NOTIFY", raising=False)
    monkeypatch.delenv("ONYX_NO_DELIVER", raising=False)
    monkeypatch.delenv("ONYX_NO_PROGRESS", raising=False)
    monkeypatch.setenv("ONYX_PROGRESS_API", api_base)
    monkeypatch.setenv("ONYX_OPENCLAW_CONFIG", str(openclaw))
    monkeypatch.setenv("ONYX_PROGRESS_CHAT_ID", "12345")


def test_needs_approval_message(onyx_env, monkeypatch) -> None:
    server, api = _start_server()
    try:
        _deliver_env(monkeypatch, onyx_env.tmp, api)
        cfg = load_config()
        job = create_job(cfg, kind="code", task="Build widgets", repo="/tmp/r")
        d = job_dir(cfg, job.id)
        d.joinpath("plan-view.md").write_text("PLAN BODY\n")
        transition(cfg, job, "needs-approval")
        from onyx_pipeline.jobs import load_job

        job = load_job(cfg, job.id)
        text = compose_stop_message(cfg, job)
        assert "PLAN BODY" in text
        assert 'Reply "approve" to build it' in text
        assert "revisions left: 3" in text
        assert deliver_stop(cfg, job)
        assert len(_Recorder.calls) >= 1
        assert "PLAN BODY" in _Recorder.calls[0][1]["text"]
    finally:
        server.shutdown()


def test_needs_answer_message(onyx_env, monkeypatch) -> None:
    server, api = _start_server()
    try:
        _deliver_env(monkeypatch, onyx_env.tmp, api)
        cfg = load_config()
        job = create_job(cfg, kind="code", task="Widget API", repo="/tmp/r")
        plan = "## OPEN QUESTIONS\n\n1. JSON or XML?\n2. Auth method?\n"
        job_dir(cfg, job.id).joinpath("plan.md").write_text(plan)
        job.state = "needs-answer"
        save_job(cfg, job)
        text = compose_stop_message(cfg, job)
        assert "Claude needs answers before it can plan: Widget API" in text
        assert "1. JSON or XML?" in text
        assert "Reply with your answers." in text
    finally:
        server.shutdown()


def test_done_message_with_auto_quote(onyx_env, monkeypatch) -> None:
    server, api = _start_server()
    try:
        _deliver_env(monkeypatch, onyx_env.tmp, api)
        cfg = load_config()
        job = create_job(cfg, kind="code", task="Ship it", repo="/tmp/r")
        job.flags["auto_quote"] = "just do it"
        job.state = "done"
        write_summary(cfg, job)
        save_job(cfg, job)
        text = compose_stop_message(cfg, job)
        assert text.startswith("Job done: Ship it")
        assert 'Plan check skipped because you said: "just do it"' in text
        assert "Nothing is pushed." in text
    finally:
        server.shutdown()


def test_chunking_long_plan_view(onyx_env, monkeypatch) -> None:
    server, api = _start_server()
    try:
        _deliver_env(monkeypatch, onyx_env.tmp, api)
        cfg = load_config()
        job = create_job(cfg, kind="code", task="Big plan", repo="/tmp/r")
        long_line = "x" * 2000
        job_dir(cfg, job.id).joinpath("plan-view.md").write_text(long_line + "\n" + long_line)
        job.state = "needs-approval"
        save_job(cfg, job)
        assert deliver_stop(cfg, job)
        send_calls = [c for c in _Recorder.calls if c[0].endswith("/sendMessage")]
        assert len(send_calls) >= 2
    finally:
        server.shutdown()


def test_quiet_hours_silent(onyx_env, monkeypatch) -> None:
    server, api = _start_server()
    try:
        _deliver_env(monkeypatch, onyx_env.tmp, api)
        monkeypatch.setenv("ONYX_QUIET_HOURS", "0-24")
        cfg = load_config()
        job = create_job(cfg, kind="research", task="Quiet task", repo=None)
        job.state = "cancelled"
        save_job(cfg, job)
        assert deliver_stop(cfg, job)
        assert _Recorder.calls[0][1]["disable_notification"] is True
    finally:
        server.shutdown()


def test_stop_sets_delivered_flag(onyx_env, monkeypatch) -> None:
    server, api = _start_server()
    try:
        _deliver_env(monkeypatch, onyx_env.tmp, api)
        cfg = load_config()
        job = create_job(cfg, kind="research", task="Finish", repo=None)
        _stop(cfg, job, "cancelled")
        from onyx_pipeline.jobs import load_job

        job = load_job(cfg, job.id)
        assert job.flags.get("delivered") == "cancelled"
        assert job.flags.get("settled") == "cancelled"
    finally:
        server.shutdown()


def test_send_failure_leaves_delivered_unset(onyx_env, monkeypatch, capsys) -> None:
    server, api = _start_server()
    try:
        _deliver_env(monkeypatch, onyx_env.tmp, api)
        cfg = load_config()
        job = create_job(cfg, kind="research", task="Fail send", repo=None)
        _Recorder.fail_send = True
        _stop(cfg, job, "cancelled")
        from onyx_pipeline.jobs import load_job

        job = load_job(cfg, job.id)
        assert job.flags.get("delivered") is None
        assert job.flags.get("settled") == "cancelled"
        assert wait_for_job(cfg, job.id, poll=0.01, sleep_fn=lambda _s: None) == 0
        out = capsys.readouterr().out
        assert "Delivered:" not in out
        assert "Next: Tell David" in out or "Next:" in out
    finally:
        _Recorder.fail_send = False
        _Recorder.fail_after_chunk = None
        server.shutdown()


def test_partial_chunk_send_not_delivered(onyx_env, monkeypatch, capsys) -> None:
    server, api = _start_server()
    try:
        _deliver_env(monkeypatch, onyx_env.tmp, api)
        cfg = load_config()
        job = create_job(cfg, kind="code", task="Big plan", repo="/tmp/r")
        long_line = "x" * 2000
        job_dir(cfg, job.id).joinpath("plan-view.md").write_text(long_line + "\n" + long_line)
        _Recorder.fail_after_chunk = 1
        _stop(cfg, job, "needs-approval")
        job = load_job(cfg, job.id)
        assert job.flags.get("delivered") is None
        assert job.flags.get("settled") == "needs-approval"
        assert wait_for_job(cfg, job.id, poll=0.01, sleep_fn=lambda _s: None) == 0
        out = capsys.readouterr().out
        assert "Delivered:" not in out
        assert "PLAN BODY" not in out
        assert "x" * 100 in out or "Next:" in out
    finally:
        _Recorder.fail_after_chunk = None
        server.shutdown()


def test_wait_polls_until_settled(onyx_env, capsys) -> None:
    job = create_job(onyx_env.cfg, kind="research", task="Settle later", repo=None)
    job.state = "done"
    job.flags["settled"] = None
    save_job(onyx_env.cfg, job)
    polls = []

    def fake_sleep(_seconds: float) -> None:
        polls.append(1)
        if len(polls) == 1:
            current = load_job(onyx_env.cfg, job.id)
            current.flags["settled"] = "done"
            current.flags["delivered"] = "done"
            save_job(onyx_env.cfg, current)

    assert wait_for_job(onyx_env.cfg, job.id, poll=0.01, sleep_fn=fake_sleep) == 0
    out = capsys.readouterr().out
    assert "Delivered:" in out
    assert len(polls) >= 1


def test_wait_settle_grace_falls_back_to_summary(onyx_env, capsys) -> None:
    from datetime import datetime, timedelta

    job = create_job(onyx_env.cfg, kind="research", task="Old unsettled", repo=None)
    job.state = "done"
    job.flags["settled"] = None
    job.stages.append(
        {
            "state": "done",
            "at": (datetime.now().astimezone() - timedelta(seconds=91)).isoformat(),
        }
    )
    save_job(onyx_env.cfg, job)
    assert wait_for_job(onyx_env.cfg, job.id, poll=0.01, sleep_fn=lambda _s: None) == 0
    out = capsys.readouterr().out
    assert "Delivered:" not in out
    assert "Next: Tell David" in out


def test_stop_skips_notify_when_delivered(onyx_env, monkeypatch) -> None:
    server, api = _start_server()
    try:
        _deliver_env(monkeypatch, onyx_env.tmp, api)
        cfg = load_config()
        job = create_job(cfg, kind="research", task="No wake", repo=None)
        notified: list[str] = []

        def _fake_notify(_cfg, j) -> None:
            notified.append(j.id)

        monkeypatch.setattr("onyx_pipeline.stages.notify_job", _fake_notify)
        _stop(cfg, job, "cancelled")
        assert notified == []
    finally:
        server.shutdown()


def test_cancel_job_delivers(onyx_env, monkeypatch) -> None:
    server, api = _start_server()
    try:
        _deliver_env(monkeypatch, onyx_env.tmp, api)
        cfg = load_config()
        job = create_job(cfg, kind="research", task="Cancel me", repo=None)
        from onyx_pipeline.stages import cancel_job

        cancelled = cancel_job(cfg, job.id)
        assert cancelled.state == "cancelled"
        job = load_job(cfg, job.id)
        assert job.flags.get("delivered") == "cancelled"
        assert job.flags.get("settled") == "cancelled"
        send_calls = [c for c in _Recorder.calls if c[0].endswith("/sendMessage")]
        assert send_calls
        assert "Job cancelled:" in send_calls[0][1]["text"]
    finally:
        server.shutdown()


def test_research_done_sends_report_text(onyx_env, monkeypatch, tmp_path) -> None:
    server, api = _start_server()
    try:
        _deliver_env(monkeypatch, onyx_env.tmp, api)
        cfg = load_config()
        report = tmp_path / "report.md"
        report.write_text("# Scout\n\nIt is a finance tracker.\n")
        job = create_job(cfg, kind="research", task="Scout the tracker repo", repo=None)
        job.state = "done"
        job.report_path = str(report)
        save_job(cfg, job)
        text = compose_stop_message(cfg, job)
        assert text.startswith("Research done: Scout the tracker repo")
        assert "It is a finance tracker." in text
        assert f"Full report: {report}" in text
        assert "push" not in text.lower()
    finally:
        server.shutdown()


def test_research_done_truncates_huge_report(onyx_env, monkeypatch, tmp_path) -> None:
    server, api = _start_server()
    try:
        _deliver_env(monkeypatch, onyx_env.tmp, api)
        cfg = load_config()
        report = tmp_path / "big.md"
        report.write_text("line\n" * 10000)
        job = create_job(cfg, kind="research", task="Big report", repo=None)
        job.state = "done"
        job.report_path = str(report)
        save_job(cfg, job)
        text = compose_stop_message(cfg, job)
        assert "[truncated; the rest is in the full report]" in text
        assert len(text) < 15000
    finally:
        server.shutdown()


def _research_job(cfg, report: Path | None, task: str = "Scout the tracker repo"):
    job = create_job(cfg, kind="research", task=task, repo=None)
    job.state = "done"
    job.report_path = str(report) if report else None
    save_job(cfg, job)
    return job


def test_research_done_sends_report_as_document(onyx_env, monkeypatch, tmp_path) -> None:
    server, api = _start_server()
    try:
        _deliver_env(monkeypatch, onyx_env.tmp, api)
        cfg = load_config()
        report = tmp_path / "scout.md"
        report.write_text("# Scout\n\nIt is a finance tracker.\n")
        assert deliver_stop(cfg, _research_job(cfg, report)) is True
        assert [p.rsplit("/", 1)[-1] for p, _ in _Recorder.calls] == ["sendDocument"]
        raw = _Recorder.calls[0][1]["_multipart"]
        assert 'filename="scout.md"' in raw
        assert "It is a finance tracker." in raw
        assert "Research done: Scout the tracker repo" in raw
    finally:
        server.shutdown()


def test_research_done_falls_back_to_text_when_upload_fails(
    onyx_env, monkeypatch, tmp_path
) -> None:
    server, api = _start_server()
    try:
        _deliver_env(monkeypatch, onyx_env.tmp, api)
        cfg = load_config()
        report = tmp_path / "scout.md"
        report.write_text("# Scout\n\nIt is a finance tracker.\n")
        _Recorder.fail_send = True
        assert deliver_stop(cfg, _research_job(cfg, report)) is False
        assert [p.rsplit("/", 1)[-1] for p, _ in _Recorder.calls] == ["sendDocument", "sendMessage"]
    finally:
        server.shutdown()


def test_research_done_empty_report_sends_text_not_document(
    onyx_env, monkeypatch, tmp_path
) -> None:
    server, api = _start_server()
    try:
        _deliver_env(monkeypatch, onyx_env.tmp, api)
        cfg = load_config()
        report = tmp_path / "empty.md"
        report.write_text("  \n")
        assert deliver_stop(cfg, _research_job(cfg, report)) is True
        assert [p.rsplit("/", 1)[-1] for p, _ in _Recorder.calls] == ["sendMessage"]
    finally:
        server.shutdown()


def test_research_done_with_missing_report(onyx_env, monkeypatch) -> None:
    server, api = _start_server()
    try:
        _deliver_env(monkeypatch, onyx_env.tmp, api)
        cfg = load_config()
        job = create_job(cfg, kind="research", task="No report", repo=None)
        job.state = "done"
        save_job(cfg, job)
        assert "report is empty or missing" in compose_stop_message(cfg, job)
    finally:
        server.shutdown()
