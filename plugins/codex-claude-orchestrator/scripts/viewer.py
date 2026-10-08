"""Read-only loopback viewer and the shared artifact reader. Execution happens only through MCP tools."""
from __future__ import annotations

import hashlib
import hmac
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets
import threading
from typing import Any
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = {
    "report": "report.md", "result": "result.json", "brief": "brief.md", "patch": "changes.patch",
    "delivery_patch": "delivery.patch", "carried_patch": "carried.patch", "outcome": "outcome.json", "packet": "packet.json", "plan": "plan.json",
    "inputs": "inputs.json", "instruction_layer": "instruction-layer.json", "final_message": "final-message.md",
    "decision": "decision.json", "decision_history": "decision-history.json", "execution": "execution.json",
    "environment": "environment.json", "command": "command.json", "stream": "stream.jsonl", "stderr": "stderr",
    "bridge_log": "bridge.log",
}
DEFAULT_PAGE_BYTES = 200_000
MAX_PAGE_BYTES = 1_000_000


def _utf8_end(data: bytes, end: int) -> int:
    """Move a page end back so it never splits a UTF-8 sequence."""
    if end >= len(data):
        return len(data)
    cursor = end
    while cursor > 0 and (data[cursor] & 0xC0) == 0x80:
        cursor -= 1
    return cursor if cursor > 0 else end


def read_artifact(runtime: Any, run_id: str, name: str, *, offset: int = 0, limit: int = DEFAULT_PAGE_BYTES) -> dict[str, Any]:
    if name not in ARTIFACTS:
        raise ValueError("unknown artifact; use one of: " + ", ".join(ARTIFACTS))
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_PAGE_BYTES:
        raise ValueError(f"limit must be 1..{MAX_PAGE_BYTES}")
    folder = runtime.run_dir(run_id).resolve()
    runtime.snapshot(run_id)
    path = folder / ARTIFACTS[name]
    if path.is_symlink() or (path.exists() and path.resolve().parent != folder):
        raise ValueError("artifact escapes the run directory")
    if not path.is_file():
        return {"name": name, "available": False, "content": None}
    data = path.read_bytes()
    end = _utf8_end(data, min(len(data), offset + limit))
    page = data[offset:end]
    answer: dict[str, Any] = {"name": name, "available": True, "size_bytes": len(data),
                              "sha256": hashlib.sha256(data).hexdigest(), "offset": offset, "next_offset": end,
                              "end_of_artifact": end >= len(data)}
    if offset == 0 and end >= len(data) and path.suffix == ".json":
        try:
            answer["content"] = json.loads(data)
            return answer
        except ValueError:
            pass
    answer["content"] = page.decode("utf-8", "replace")
    return answer


def _authorized(header: Any, token: str) -> bool:
    """Compare as bytes: compare_digest raises TypeError for non-ASCII str."""
    supplied = header.encode("latin-1", "replace") if isinstance(header, str) else b""
    return hmac.compare_digest(supplied, ("Bearer " + token).encode("ascii"))


class Viewer:
    def __init__(self, runtime: Any):
        self.runtime = runtime
        self.token = secrets.token_urlsafe(32)
        self.http: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> "Viewer":
        viewer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: Any) -> None:
                pass  # Never log URLs, capability tokens or task contents.

            def send(self, status: int, payload: Any, content_type: str = "application/json; charset=utf-8") -> None:
                body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; "
                                 "style-src 'unsafe-inline'; connect-src 'self'; img-src data:; base-uri 'none'; frame-ancestors 'none'")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_GET(self) -> None:
                assert viewer.http is not None
                port = viewer.http.server_port
                hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
                if self.headers.get("Host") not in hosts:
                    return self.send(403, {"error": "Invalid host"})
                origin = self.headers.get("Origin")
                if origin and origin not in {"http://" + host for host in hosts}:
                    return self.send(403, {"error": "Invalid origin"})
                route = urlparse(self.path)
                if route.path == "/":
                    try:
                        body = (ROOT / "assets/dashboard.html").read_bytes()
                    except OSError:
                        return self.send(500, {"error": "Unable to read the viewer page"})
                    return self.send(200, body, "text/html; charset=utf-8")
                if not _authorized(self.headers.get("Authorization"), viewer.token):
                    return self.send(401, {"error": "Reopen the details link supplied by Claude tools."})
                query = parse_qs(route.query)
                run_id = query.get("run_id", [""])[0]
                try:
                    if route.path == "/api/runs":
                        limit = int(query.get("limit", [100])[0])
                        offset = int(query.get("offset", [0])[0])
                        if not 1 <= limit <= 200 or offset < 0:
                            raise ValueError("Invalid history page")
                        rows = viewer.runtime.list_runs(limit=limit + 1, offset=offset)
                        return self.send(200, {"runs": rows[:limit], "next_offset": offset + min(limit, len(rows)),
                                               "has_more": len(rows) > limit})
                    if route.path == "/api/snapshot":
                        return self.send(200, viewer.runtime.snapshot(run_id))
                    if route.path == "/api/events":
                        after, limit = int(query.get("after", [0])[0]), int(query.get("limit", [200])[0])
                        if after < 0 or not 1 <= limit <= 200:
                            raise ValueError("Invalid event page")
                        return self.send(200, viewer.runtime.events(run_id, after=after, limit=limit))
                    if route.path == "/api/artifact":
                        paging = {key: int(query[key][0]) for key in ("offset", "limit") if key in query}
                        return self.send(200, read_artifact(viewer.runtime, run_id, query.get("name", [""])[0], **paging))
                    return self.send(404, {"error": "Not found"})
                except (ValueError, RuntimeError, KeyError, FileNotFoundError) as exc:
                    return self.send(400, {"error": str(exc)})
                except OSError as exc:
                    return self.send(500, {"error": f"{type(exc).__name__}: {exc}"})

            def do_POST(self) -> None:
                self.send(405, {"error": "Read-only viewer; send instructions through Codex."})

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.http.daemon_threads = True
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        return self

    def url(self, run_id: str = "") -> str:
        if self.http is None:
            raise RuntimeError("Viewer not started")
        return f"http://127.0.0.1:{self.http.server_port}/#token={self.token}&run={run_id}"

    def close(self) -> None:
        if self.http:
            self.http.shutdown()
            self.http.server_close()
            self.http = None
        if self.thread:
            self.thread.join(timeout=3)
