from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from mailops.singleton import _pid_alive


def test_current_process_is_alive_and_bogus_pid_is_not():
    assert _pid_alive(os.getpid())
    assert not _pid_alive(0)
    assert not _pid_alive(-5)


@pytest.mark.skipif(os.name != "nt", reason="Windows keeps exited processes openable while a handle exists")
def test_exited_process_with_open_handle_is_not_alive():
    """Regression: restarting right after killing the worker failed with
    'mailops already running (pid N)' because the dead worker could still be
    opened while its launcher held a handle."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    deadline = time.time() + 20
    while proc.poll() is None and time.time() < deadline:
        time.sleep(0.05)
    # proc object (and its OS handle) still exists here; do not wait()/close.
    assert not _pid_alive(proc.pid)
