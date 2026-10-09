"""Config reads ONYX_LIMIT_CURSOR and ONYX_MAX_PARTS."""

from __future__ import annotations

from onyx_pipeline.config import load_config


def test_limit_cursor_and_max_parts_defaults(onyx_env) -> None:
    cfg = load_config()
    assert cfg.limit_cursor == 3
    assert cfg.max_parts == 3


def test_limit_cursor_and_max_parts_from_env(onyx_env, monkeypatch) -> None:
    monkeypatch.setenv("ONYX_LIMIT_CURSOR", "2")
    monkeypatch.setenv("ONYX_MAX_PARTS", "2")
    cfg = load_config()
    assert cfg.limit_cursor == 2
    assert cfg.max_parts == 2


def test_settings_from_config_file(onyx_env, monkeypatch, tmp_path) -> None:
    path = tmp_path / "config.env"
    path.write_text("ONYX_LIMIT_CURSOR=1\nONYX_MAX_PARTS=2\n")
    monkeypatch.setenv("ONYX_CONFIG_FILE", str(path))
    monkeypatch.delenv("ONYX_LIMIT_CURSOR", raising=False)
    monkeypatch.delenv("ONYX_MAX_PARTS", raising=False)
    cfg = load_config()
    assert cfg.limit_cursor == 1
    assert cfg.max_parts == 2
