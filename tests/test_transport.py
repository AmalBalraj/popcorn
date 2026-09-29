import http.server
import threading
import time
from pathlib import Path

import pytest

from sources.health import ProviderError
from sources.transport import fetch_response


def exchange(url, timeout=2):
    return fetch_response(
        url,
        params={},
        headers={},
        connect_timeout=0.5,
        read_timeout=0.5,
        total_timeout=timeout,
        max_bytes=1024,
    )


def test_absolute_deadline_kills_stuck_http_worker(monkeypatch):
    monkeypatch.setattr(
        "sources.transport.HTTP_WORKER",
        Path(__file__).parent / "fixtures/stalled_http.py",
    )
    started = time.monotonic()
    with pytest.raises(ProviderError, match="deadline"):
        exchange("https://never-requested.invalid", timeout=0.1)
    assert time.monotonic() - started < 0.5


@pytest.fixture
def local_http():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/trickle":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                try:
                    for _ in range(30):
                        self.wfile.write(b" ")
                        self.wfile.flush()
                        time.sleep(0.05)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            else:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"results": []}')

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_real_http_child_returns_valid_response(local_http):
    response = exchange(local_http)
    assert response.status_code == 200
    assert response.json() == {"results": []}


def test_trickling_body_cannot_exceed_absolute_deadline(local_http):
    started = time.monotonic()
    with pytest.raises(ProviderError, match="deadline|timeout"):
        exchange(local_http + "/trickle", timeout=0.3)
    assert time.monotonic() - started < 0.7
