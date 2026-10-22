"""Private loopback HTTP API for the Omega capability."""
from __future__ import annotations

import hmac
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

from .contracts import ContractError
from .jobs import RequestConflict

MAX_BODY = 65_536


def serve(service, token: str, host: str = "127.0.0.1", port: int = 0) -> HTTPServer:
    if not token or len(token) < 16:
        raise ValueError("service token must be at least 16 characters")
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise ValueError("reference service must bind to loopback")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            # Never log Authorization or request bodies.
            pass

        def _send(self, code: int, data: dict):
            body = json.dumps(data, sort_keys=True).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            supplied = self.headers.get("Authorization", "")
            if not hmac.compare_digest(supplied, "Bearer " + token):
                self._send(401, {"error_code": "UNAUTHORIZED"})
                return False
            return True

        def do_GET(self):
            if self.path == "/health":
                self._send(200, {"status": "ok"})
                return
            if not self._authorized():
                return
            prefix = "/v1/operations/"
            if not self.path.startswith(prefix):
                self._send(404, {"error_code": "NOT_FOUND"})
                return
            result = service.jobs.get(self.path[len(prefix):], service.workspace)
            self._send(200 if result else 404, result or {"error_code": "NOT_FOUND"})

        def do_POST(self):
            if not self._authorized():
                return
            if self.path != "/v1/operations":
                self._send(404, {"error_code": "NOT_FOUND"})
                return
            try:
                size = int(self.headers.get("Content-Length", "-1"))
                if not 0 <= size <= MAX_BODY:
                    raise ContractError("request body size is invalid")
                raw = json.loads(self.rfile.read(size))
                result = service.submit(raw)
                self._send(200 if result["phase"] == "finished" else 422, result)
            except RequestConflict as exc:
                self._send(409, {"error_code": "REQUEST_CONFLICT", "summary": str(exc)})
            except (ContractError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                self._send(400, {"error_code": "INVALID_REQUEST", "summary": str(exc)[:500]})
            except Exception:
                # A partially committed operation remains resumable by its
                # durable request_id. Do not turn an unknown error into success.
                self._send(500, {"error_code": "RETRYABLE_INTERNAL"})

    # One request at a time keeps the shared SQLite connections' transaction
    # boundaries deterministic. Long workers must run as separate durable jobs.
    return HTTPServer((host, port), Handler)
