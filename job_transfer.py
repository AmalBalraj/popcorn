"""Attach to an existing transfer or resume its original aria2 directory."""

import fcntl
import json
import os
import signal
import re
import subprocess
import sys
import time
from pathlib import Path

PROGRESS_PATTERN = re.compile(
    r"(?P<done>[\d.]+\s*[KMGTP]?i?B)/(?P<total>[\d.]+\s*[KMGTP]?i?B)"
    r"\((?P<pct>\d+)%\).*?DL:(?P<speed>[\d.]+\s*[KMGTP]?i?B)"
    r"(?:.*?ETA:(?P<eta>[0-9dhms]+))?",
    re.I,
)


def transfer_state(directory):
    try:
        return json.loads((Path(directory) / ".transfer.json").read_text())
    except (OSError, ValueError):
        return {}


def transfer_locked(directory):
    with (Path(directory) / ".transfer.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True


def owned_runner(pid, directory):
    try:
        args = Path(f"/proc/{int(pid)}/cmdline").read_bytes().split(b"\0")
        return (
            any(a.endswith(b"/transfer_runner.py") for a in args)
            and str(Path(directory).resolve()).encode() in args
        )
    except (OSError, ValueError, TypeError):
        return False


def stop_transfer(directory):
    state = transfer_state(directory)
    pid = state.get("pid")
    # The supervisor takes its lock before publishing its atomic identity.
    identity_deadline = time.monotonic() + 2
    while (
        transfer_locked(directory)
        and not owned_runner(pid, directory)
        and time.monotonic() < identity_deadline
    ):
        time.sleep(0.02)
        pid = transfer_state(directory).get("pid")
    if transfer_locked(directory) and not owned_runner(pid, directory):
        raise RuntimeError(
            "The transfer owner cannot be verified; its local files are kept."
        )
    if transfer_locked(directory) and owned_runner(pid, directory):
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 30
        while transfer_locked(directory) and time.monotonic() < deadline:
            time.sleep(0.1)
        if transfer_locked(directory):
            raise RuntimeError(
                "The transfer is still stopping; its local files are kept."
            )


class Transfer:
    def __init__(self, directory, command):
        self.directory = Path(directory)
        self.child = None
        if not transfer_locked(directory):
            # A previous successful exit is authoritative even if the web
            # process died before committing download_complete to SQLite.
            if transfer_state(directory).get("exit_code") != 0:
                self.child = subprocess.Popen(
                    [
                        sys.executable,
                        str(Path(__file__).with_name("transfer_runner.py")),
                        str(directory),
                        *command,
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                # Wait for the supervisor to take its lock/write identity.
                for _ in range(100):
                    if transfer_locked(directory) or self.child.poll() is not None:
                        break
                    time.sleep(0.02)
        self.pid = transfer_state(directory).get("pid")
        if (
            not owned_runner(self.pid, directory)
            and self.child
            and self.child.poll() is None
        ):
            self.pid = self.child.pid

    def poll(self):
        if transfer_locked(self.directory):
            return None
        if self.child and self.child.poll() is None:
            return None
        if self.child:
            self.child.poll()  # reap the supervisor when it belongs to us
        code = transfer_state(self.directory).get("exit_code")
        return code if code is not None else 1

    def wait(self):
        while self.poll() is None:
            time.sleep(0.2)
        return self.poll()

    def terminate(self):
        stop_transfer(self.directory)

    def progress(self):
        try:
            with (self.directory / ".transfer.log").open("rb") as handle:
                handle.seek(max(0, os.fstat(handle.fileno()).st_size - 4096))
                return handle.read().decode(errors="replace")
        except OSError:
            return ""
