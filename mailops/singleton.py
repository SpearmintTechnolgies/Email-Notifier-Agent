"""Ensure only one mailops poller owns the Telegram token."""
from __future__ import annotations

import atexit
import os
from pathlib import Path

from .paths import DATA_DIR

LOCK_PATH = DATA_DIR / "mailops.pid"
_lock_fh = None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
        if not handle:
            return False
        try:
            # A terminated process whose handle is still open elsewhere (e.g.
            # its launcher) can still be opened: only STILL_ACTIVE means alive.
            code = ctypes.c_ulong(0)
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == 259  # STILL_ACTIVE
            return True
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def acquire_or_exit() -> None:
    global _lock_fh
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if LOCK_PATH.exists():
        try:
            old = int(LOCK_PATH.read_text(encoding="utf-8").strip())
        except ValueError:
            old = 0
        if old and old != os.getpid() and _pid_alive(old):
            raise SystemExit(
                f"mailops already running (pid {old}). Stop it before starting another instance."
            )
    _lock_fh = open(LOCK_PATH, "w", encoding="utf-8")
    _lock_fh.write(str(os.getpid()))
    _lock_fh.flush()
    atexit.register(release)


def release() -> None:
    global _lock_fh
    try:
        if _lock_fh:
            _lock_fh.close()
            _lock_fh = None
        if LOCK_PATH.exists():
            text = LOCK_PATH.read_text(encoding="utf-8").strip()
            if text == str(os.getpid()):
                LOCK_PATH.unlink(missing_ok=True)
    except OSError:
        pass
