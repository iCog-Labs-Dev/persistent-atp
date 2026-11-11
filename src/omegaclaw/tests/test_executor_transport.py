"""Provider response strictness and a total deadline for slowly streamed bodies."""
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time

import pytest

from mathproof.http_atp import ATPServiceError
from omegaclaw.execution import BoundedHTTPFormalATP


@pytest.mark.parametrize("body", [b'{"a":1,"a":2}', b'{"value":NaN}'])
def test_invalid_json_cannot_become_provider_evidence(body):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        adapter = BoundedHTTPFormalATP(f"http://127.0.0.1:{server.server_port}")
        with pytest.raises(ATPServiceError, match="invalid JSON"):
            adapter._call("GET", "/result")
    finally:
        server.shutdown()
        server.server_close()


def test_streamed_body_cannot_extend_the_total_request_deadline():
    stopped = threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            with suppress(OSError):
                for _ in range(100):
                    if stopped.wait(.01): break
                    self.wfile.write(b" ")
                    self.wfile.flush()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        adapter = BoundedHTTPFormalATP(f"http://127.0.0.1:{server.server_port}", timeout=.1)
        start = time.monotonic()
        with pytest.raises(ATPServiceError): adapter._call("GET", "/slow-result")
        assert time.monotonic() - start < .7
    finally:
        stopped.set()
        server.shutdown()
        server.server_close()
