"""Tests for the parent-death watchdog in main.py."""

import os
import pathlib
import subprocess
import sys
import time

from main import parent_has_changed, watch_parent

PKG_DIR = str(pathlib.Path(__file__).resolve().parent.parent)


def test_parent_unchanged_is_not_flagged():
    assert parent_has_changed(1234, 1234) is False


def test_reparenting_is_flagged():
    # Re-parented to init or to a subreaper like `systemd --user`
    assert parent_has_changed(1234, 1) is True
    assert parent_has_changed(1234, 1880) is True


def test_watch_parent_returns_daemon_thread():
    thread = watch_parent(poll_seconds=3600)
    assert thread.daemon is True
    assert thread.is_alive()


def test_orphaned_child_exits():
    """A grandchild running watch_parent must exit once its parent dies."""
    orphan_code = (
        "import sys, time;"
        f"sys.path.insert(0, {PKG_DIR!r});"
        "from main import watch_parent;"
        "watch_parent(poll_seconds=0.1);"
        "print('started', flush=True);"
        "time.sleep(30)"
    )
    middle_code = (
        "import subprocess, sys;"
        f"child = subprocess.Popen([sys.executable, '-c', {orphan_code!r}],"
        " stdout=subprocess.PIPE, text=True);"
        "child.stdout.readline();"
        "print(child.pid, flush=True)"
        # The middle process exits here, orphaning the grandchild
    )
    out = subprocess.run([sys.executable, "-c", middle_code], capture_output=True, text=True, timeout=30)
    orphan_pid = int(out.stdout.strip())
    # The orphan should self-exit within a couple of seconds
    for _ in range(50):
        if not _pid_alive(orphan_pid):
            return
        time.sleep(0.1)
    os.kill(orphan_pid, 9)
    raise AssertionError(f"orphaned process {orphan_pid} did not exit")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
