import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from ctrunner.probe import ProbeResult, probe


@contextmanager
def serve(code: int) -> Iterator[str]:
    class H(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["content-length"]))
            ok = self.headers["authorization"] == "Bearer sk-good-0000"
            self.send_response(code if ok else 401)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a: object) -> None: ...

    srv = HTTPServer(("127.0.0.1", 0), H)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join()


@pytest.fixture
def ok_url() -> Iterator[str]:
    with serve(200) as url:
        yield url


def test_ok(ok_url: str) -> None:
    assert probe(ok_url, "sk-good-0000", "m", timeout_s=5) is ProbeResult.OK


def test_rejected(ok_url: str) -> None:
    assert probe(ok_url, "sk-bad-00000", "m", timeout_s=5) is ProbeResult.REJECTED


def test_unreachable() -> None:
    assert probe("http://127.0.0.1:9", "sk-good-0000", "m", timeout_s=1) is ProbeResult.UNREACHABLE


def test_upstream_error() -> None:
    with serve(503) as url:
        assert probe(url, "sk-good-0000", "m", timeout_s=5) is ProbeResult.UNREACHABLE


def test_redirect_is_not_followed() -> None:
    seen: list[str | None] = []

    class Sink(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append(self.headers["authorization"])
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a: object) -> None: ...

    sink = HTTPServer(("127.0.0.1", 0), Sink)

    class Redirect(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["content-length"]))
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{sink.server_port}/")
            self.end_headers()

        def log_message(self, *a: object) -> None: ...

    src = HTTPServer(("127.0.0.1", 0), Redirect)
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (sink, src)]
    for t in threads:
        t.start()
    try:
        url = f"http://127.0.0.1:{src.server_port}"
        assert probe(url, "sk-good-0000", "m", timeout_s=5) is ProbeResult.UNREACHABLE
        assert seen == []
    finally:
        for s in (sink, src):
            s.shutdown()
            s.server_close()
        for t in threads:
            t.join()
