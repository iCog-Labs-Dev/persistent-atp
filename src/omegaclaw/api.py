"""Private loopback HTTP API for the Omega capability."""
from __future__ import annotations

import hmac
import asyncio
import base64
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from persistent_atp_client.codecs import request_from_json, to_dict
from persistent_atp_client.models import ArtifactRef, OperationRef
from persistent_atp_client.errors import ClientError

from .contracts import ContractError
from .jobs import RequestConflict

MAX_BODY = 65_536


def _strict_json(data: bytes):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON value")))


def serve(service, token: str, host: str = "127.0.0.1", port: int = 0, *, receiver=None) -> HTTPServer:
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
            if self.path.startswith("/v2/") and receiver is not None:
                return self._v2_get()
            prefix = "/v1/operations/"
            if not self.path.startswith(prefix):
                self._send(404, {"error_code": "NOT_FOUND"})
                return
            result = service.jobs.get(self.path[len(prefix):], service.workspace)
            self._send(200 if result else 404, result or {"error_code": "NOT_FOUND"})

        def do_POST(self):
            if not self._authorized():
                return
            if self.path.startswith("/v2/") and receiver is not None:
                return self._v2_post()
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

        def _v2_get(self):
            try:
                path = urlsplit(self.path)
                parts = [unquote(item) for item in path.path.split("/") if item]
                query = parse_qs(path.query)
                if parts == ["v2", "capabilities"]:
                    data = asyncio.run(receiver.capabilities())
                elif len(parts) == 4 and parts[:2] == ["v2", "requests"]:
                    data = asyncio.run(receiver.lookup_request(parts[2], parts[3]))
                elif len(parts) == 4 and parts[:2] == ["v2", "operations"]:
                    data = asyncio.run(receiver.get_operation(OperationRef(parts[2], parts[3])))
                elif len(parts) == 3 and parts[:2] == ["v2", "operations"]:
                    data = asyncio.run(receiver.list_operations(workspace_id=parts[2],
                        proof_id=query.get("proof_id", [""])[0] or None,
                        active_only=query.get("active_only", ["true"])[0] == "true",
                        cursor=query.get("cursor", [""])[0] or None))
                elif len(parts) == 4 and parts[:2] == ["v2", "status"]:
                    revision = query.get("at_revision", [None])[0]
                    data = asyncio.run(receiver.read_status(workspace_id=parts[2], proof_id=parts[3],
                        at_revision=int(revision) if revision is not None else None))
                elif len(parts) == 4 and parts[:2] == ["v2", "events"]:
                    events, cursor = asyncio.run(receiver.read_events(OperationRef(parts[2], parts[3]),
                        query.get("cursor", [None])[0]))
                    data = {"events": [to_dict(event) for event in events], "cursor": cursor}
                elif len(parts) == 4 and parts[:2] == ["v2", "artifacts"]:
                    content = asyncio.run(receiver.get_artifact(ArtifactRef(parts[3]), workspace_id=parts[2]))
                    data = {"content_base64": base64.b64encode(content).decode("ascii")}
                else:
                    self._send(404, {"error_code": "NOT_FOUND"})
                    return
                self._send(200, to_dict(data) if hasattr(data, "__dataclass_fields__") else
                           [to_dict(item) for item in data] if isinstance(data, tuple) else data)
            except KeyError:
                self._send(404, {"error_code": "NOT_FOUND"})
            except ClientError as exc:
                self._send(403 if exc.code == "FORBIDDEN" else 422, {"error_code": exc.code})
            except (ValueError, TypeError):
                self._send(400, {"error_code": "INVALID_REQUEST"})
            except Exception:
                self._send(500, {"error_code": "RETRYABLE_INTERNAL"})

        def _v2_post(self):
            try:
                size = int(self.headers.get("Content-Length", "-1"))
                if not 0 <= size <= 20 * 1024 * 1024:
                    raise ValueError("invalid request size")
                body = self.rfile.read(size)
                if self.path == "/v2/requests":
                    data = asyncio.run(receiver.submit(request_from_json(body)))
                elif self.path == "/v2/artifacts":
                    raw = _strict_json(body)
                    if not isinstance(raw, dict) or set(raw) != {"workspace_id", "media_type", "expected_hash", "content_base64"}:
                        raise ValueError("invalid artifact request")
                    content = base64.b64decode(raw["content_base64"], validate=True)
                    data = asyncio.run(receiver.put_artifact(content, workspace_id=raw["workspace_id"],
                        media_type=raw["media_type"], expected_hash=raw["expected_hash"]))
                else:
                    self._send(404, {"error_code": "NOT_FOUND"})
                    return
                self._send(200, to_dict(data))
            except ClientError as exc:
                code = 409 if exc.code == "IDEMPOTENCY_CONFLICT" else 403 if exc.code == "FORBIDDEN" else 422
                self._send(code, {"error_code": exc.code})
            except (ValueError, TypeError, KeyError):
                self._send(400, {"error_code": "INVALID_REQUEST"})
            except Exception:
                self._send(500, {"error_code": "RETRYABLE_INTERNAL"})

    # One request at a time keeps the shared SQLite connections' transaction
    # boundaries deterministic. Long workers must run as separate durable jobs.
    return HTTPServer((host, port), Handler)
