"""Tests must never append to the live data/mailops.log."""
from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

import mailops.paths as paths
from mailops.main import _setup_logging

MARKER = "UNIQUE-SETUP-LOGGING-ISOLATION-c41d"


def test_log_file_refuses_live_path_under_pytest(monkeypatch):
    monkeypatch.delenv("MAILOPS_LOG_FILE", raising=False)
    with pytest.raises(RuntimeError, match="refusing to log"):
        paths.log_file()


def test_log_file_honours_override(tmp_path, monkeypatch):
    dest = tmp_path / "isolated.log"
    monkeypatch.setenv("MAILOPS_LOG_FILE", str(dest))
    assert paths.log_file() == dest.resolve()


def test_setup_logging_writes_only_to_the_temp_log(tmp_path, monkeypatch):
    dest = tmp_path / "isolated.log"
    monkeypatch.setenv("MAILOPS_LOG_FILE", str(dest))
    live_before = (
        paths.LIVE_LOG_FILE.read_text(encoding="utf-8", errors="replace")
        if paths.LIVE_LOG_FILE.exists()
        else ""
    )
    root = logging.getLogger()
    old = list(root.handlers)
    try:
        _setup_logging()
        logging.getLogger("mailops").info(MARKER)
        assert dest.exists()
        assert MARKER in dest.read_text(encoding="utf-8")
        live_after = (
            paths.LIVE_LOG_FILE.read_text(encoding="utf-8", errors="replace")
            if paths.LIVE_LOG_FILE.exists()
            else ""
        )
        assert MARKER not in live_after
        # Anything the live agent appended is fine; the marker must not appear in the delta either.
        assert MARKER not in live_after[len(live_before) :]
    finally:
        for h in list(root.handlers):
            try:
                h.close()
            except Exception:
                pass
        root.handlers.clear()
        for h in old:
            root.addHandler(h)
