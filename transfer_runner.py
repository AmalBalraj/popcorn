"""Detached aria2 supervisor. Durable exit record closes the web-crash window.

Only invoked by Popcorn with an owned job directory. A separate flock prevents
two recovered web workers from starting the same transfer simultaneously.
"""

import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from job_transfer import PROGRESS_PATTERN


def write_state(directory, payload):
    target = directory / ".transfer.json"
    temporary = directory / ".transfer.json.tmp"
    with temporary.open("w") as handle:
        json.dump(payload, handle)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(target)


def main():
    directory = Path(sys.argv[1]).resolve()
    with (directory / ".transfer.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 75
        state = {"pid": os.getpid(), "started_at": time.time(), "exit_code": None}
        write_state(directory, state)
        process = None
        stopping = False

        def stop(signum, frame):
            nonlocal stopping
            stopping = True
            if process and process.poll() is None:
                process.terminate()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        with (directory / ".transfer.log").open("w") as log:
            try:
                if stopping:
                    state["exit_code"] = -signal.SIGTERM
                else:
                    process = subprocess.Popen(
                        sys.argv[2:],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        errors="replace",
                        bufsize=1,
                    )
                    if stopping:
                        process.terminate()
                    # Persist numeric progress only. Command failures can echo
                    # credential-bearing tracker/remote URLs; do not log them.
                    line = ""
                    while char := process.stdout.read(1):
                        if char in {"\r", "\n"}:
                            match = PROGRESS_PATTERN.search(line)
                            if match:
                                values = match.groupdict()
                                log.seek(0)
                                log.truncate()
                                log.write(
                                    f"{values['done']}/{values['total']}({values['pct']}%) DL:{values['speed']} ETA:{values.get('eta') or '0s'}\n"
                                )
                                log.flush()
                            line = ""
                        elif len(line) < 4096:
                            line += char
                    state["exit_code"] = process.wait()
                    if stopping:
                        state["exit_code"] = -signal.SIGTERM
                    elif (
                        state["exit_code"] == 0
                        and Path(sys.argv[2]).name == "aria2c"
                        and any(directory.rglob("*.aria2"))
                    ):
                        # Never mistake a clean signal exit with unfinished
                        # control files for a completed torrent.
                        state["exit_code"] = 1
            except OSError:
                state["exit_code"] = 127
        state["finished_at"] = time.time()
        write_state(directory, state)
        return state["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
