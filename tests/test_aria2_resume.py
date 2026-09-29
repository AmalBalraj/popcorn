"""Actual aria2 resume using generated, authorized data and a localhost webseed."""

import hashlib
import http.server
import re
import shutil
import threading
import time

import pytest

from job_transfer import Transfer, stop_transfer


def bencode(value):
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, dict):
        return (
            b"d"
            + b"".join(bencode(k) + bencode(v) for k, v in sorted(value.items()))
            + b"e"
        )
    return b"l" + b"".join(bencode(v) for v in value) + b"e"


@pytest.mark.skipif(not shutil.which("aria2c"), reason="aria2c is not installed")
def test_real_aria2_stop_and_resume_verified_pieces(tmp_path):
    payload = bytes(range(256)) * 2048
    piece_length = 16384
    requests = []

    class Seed(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            start, end = 0, len(payload) - 1
            match = re.match(r"bytes=(\d+)-(\d*)", self.headers.get("Range", ""))
            if match:
                start, end = int(match[1]), int(match[2]) if match[2] else end
            requests.append(start)
            self.send_response(206 if match else 200)
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Accept-Ranges", "bytes")
            if match:
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(payload)}")
            self.end_headers()
            try:
                self.wfile.write(payload[start : end + 1])
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Seed)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    directory = tmp_path / "download"
    directory.mkdir()
    torrent = tmp_path / "authorized.torrent"
    torrent.write_bytes(
        bencode(
            {
                b"info": {
                    b"name": b"fixture.mp4",
                    b"length": len(payload),
                    b"piece length": piece_length,
                    b"pieces": b"".join(
                        hashlib.sha1(payload[n : n + piece_length]).digest()
                        for n in range(0, len(payload), piece_length)
                    ),
                },
                b"url-list": [
                    f"http://127.0.0.1:{server.server_port}/fixture.mp4".encode()
                ],
            }
        )
    )
    command = [
        "aria2c",
        str(torrent),
        f"--dir={directory}",
        "--seed-time=0",
        "--enable-dht=false",
        "--enable-dht6=false",
        "--enable-peer-exchange=false",
        "--bt-enable-lpd=false",
        "--listen-port=16991-16999",
        "--file-allocation=none",
        "--continue=true",
        "--check-integrity=true",
        "--auto-save-interval=1",
        "--summary-interval=1",
        "--max-download-limit=64K",
    ]
    transfer = None
    try:
        transfer = Transfer(directory, command)
        media = directory / "fixture.mp4"
        state = directory / "fixture.mp4.aria2"
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            if (
                media.exists()
                and media.stat().st_size >= piece_length
                and state.exists()
            ):
                break
            time.sleep(0.1)
        assert media.exists() and state.exists(), transfer.progress()
        assert transfer.poll() is None, transfer.progress()
        stop_transfer(directory)
        assert transfer.wait() != 0
        assert state.exists()
        before = len(requests)
        resumed = Transfer(
            directory, [x for x in command if not x.startswith("--max-download-limit")]
        )
        transfer = resumed
        deadline = time.monotonic() + 10
        while resumed.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        assert resumed.poll() == 0, resumed.progress()
        assert media.read_bytes() == payload
        assert not state.exists()
        assert any(start > 0 for start in requests[before:]), requests
    finally:
        if transfer and transfer.poll() is None:
            stop_transfer(directory)
        server.shutdown()
        server.server_close()
