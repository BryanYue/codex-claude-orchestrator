"""Read-only loopback viewer. Execution is only exposed through the MCP tools."""
from __future__ import annotations

import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets
import sys
import threading
from urllib.parse import parse_qs, urlparse

try:
    import cli_updates
except ModuleNotFoundError:  # A partially upgraded plugin still serves its immutable run evidence.
    cli_updates = None

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = {
    "review_report": "review-report.json", "review_workspace": "review-workspace.json",
    "packet": "packet.json", "result": "result.json", "receipt": "receipt.json",
    "environment": "environment.json", "decision": "decision.json", "decision_history": "decision-history.json",
    "git_before": "git_before.json", "git_after": "git_after.json", "diff": "diff.patch",
    "workspace_before": "workspace_before.json", "workspace_after": "workspace_after.json",
    "reconciliation": "reconciliation.json", "content_binding": "content-binding.json",
}
# Captured saved-Workflow output, served in UTF-8-safe byte pages after a hash check.
PAGED_ARTIFACTS = {"workflow_report": "report", "workflow_envelope": "envelope"}

_MAINTENANCE_FIELDS = frozenset({
    "policy", "version_management", "state", "reason", "message", "next_action", "notice_pending",
    "current_version", "version_note", "error",
})
_WORKER_LOCK_STATES = frozenset({"absent", "free", "held", "unreadable"})


def read_cli_maintenance() -> dict:
    """Return only the local CLI status read model; this endpoint never starts work."""
    if cli_updates is None:
        return {"state": "unavailable", "reason": "local CLI status module is unavailable"}
    try:
        value = cli_updates.status()
    except Exception as exc:
        return {"state": "unavailable", "reason": f"{type(exc).__name__}: {exc}"}
    if not isinstance(value, dict):
        return {"state": "unavailable", "reason": "local CLI status had an invalid shape"}
    public = {key: value[key] for key in _MAINTENANCE_FIELDS if key in value}
    local = value.get("local_cli")
    if isinstance(local, dict):
        public["local_cli"] = {key: local[key] for key in ("path", "source") if key in local}
    legacy = value.get("legacy_managed_state")
    if isinstance(legacy, dict):
        running = legacy.get("maintenance_worker_running")
        public["legacy_managed_state"] = {
            "present": bool(legacy.get("present")), "ignored_for_dispatch": True,
            "retained_versions": len(legacy.get("versions") or []),
            # An unobserved lock stays None; bool() would report it as stopped.
            "maintenance_worker_running": running if isinstance(running, bool) else None,
        }
        lock = legacy.get("maintenance_worker_lock")
        if lock in _WORKER_LOCK_STATES:
            public["legacy_managed_state"]["maintenance_worker_lock"] = lock
    return public


def _authorized(header, token: str) -> bool:
    """Compare as bytes: compare_digest raises TypeError for non-ASCII str.

    http.server decodes header bytes as latin-1, so encoding back is lossless.
    """
    supplied = header.encode("latin-1", "replace") if isinstance(header, str) else b""
    return hmac.compare_digest(supplied, ("Bearer " + token).encode("ascii"))


def _workflow_delivery():
    scripts = str(ROOT / "skills/codex-claude-orchestrator/scripts")
    if scripts not in sys.path:
        sys.path.append(scripts)
    import workflow_delivery
    return workflow_delivery


def read_artifact(runtime, run_id: str, name: str, *, index: int = 0, offset: int = 0, limit: int | None = None) -> dict:
    if name == "review_report":
        snapshot = runtime.snapshot(run_id)
        result = snapshot.get("result") if isinstance(snapshot.get("result"), dict) else {}
        report = result.get("review_report")
        answer = {"name": name, "content": None}
        delivery = _workflow_delivery()
        # Capture metadata binds this canonical run artifact; CLI paths are never followed.
        if isinstance(index, bool) or not isinstance(index, int) or index != 0:
            raise ValueError("review_report index must be 0")
        if not isinstance(report, dict):
            return {**answer, "available": False, "state": "not_recorded"}
        if report.get("status") != "delivered":
            return {**answer, "available": False, "state": "not_collected"}
        recorded = {"path": report.get("path"), "size_bytes": report.get("bytes"), "sha256": report.get("sha256")}
        return delivery.read_captured_page(Path(snapshot["run_dir"]), recorded,
                                          expected_path=ARTIFACTS[name], answer=answer, offset=offset,
                                          limit=delivery.DEFAULT_PAGE_BYTES if limit is None else limit,
                                          representation="exact_json")
    if name in PAGED_ARTIFACTS:
        snapshot = runtime.snapshot(run_id)
        result = snapshot.get("result") if isinstance(snapshot.get("result"), dict) else {}
        delivery = _workflow_delivery()
        return delivery.read_page(Path(snapshot["run_dir"]), result.get("workflow_delivery"), index=index,
                                  part=PAGED_ARTIFACTS[name], offset=offset,
                                  limit=delivery.DEFAULT_PAGE_BYTES if limit is None else limit)
    if name not in ARTIFACTS:
        raise ValueError("Unknown artifact")
    snapshot = runtime.snapshot(run_id)
    folder = Path(snapshot["run_dir"]).resolve()
    path = folder / ARTIFACTS[name]
    if path.is_symlink() or (path.exists() and path.resolve().parent != folder):
        raise ValueError("Artifact escapes run directory")
    if not path.is_file():
        return {"name": name, "available": False, "state": "missing", "content": None}
    limit = 300_000
    with path.open("rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        data = handle.read(limit + 1)
    clipped = len(data) > limit
    value = data[:limit].decode("utf-8", "replace")
    if path.suffix == ".json" and not clipped:
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {"name": name, "available": False, "state": "writing", "content": None,
                    "note": "Artifact is being written or incomplete; retry."}
    return {"name": name, "available": True, "state": "available", "content": value, "truncated": clipped,
            "size_bytes": size, "display_limit_bytes": limit}


class Viewer:
    def __init__(self, runtime):
        self.runtime = runtime
        self.token = secrets.token_urlsafe(32)
        self.http = None
        self.thread = None

    def start(self):
        viewer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass  # Never log URLs, capability tokens, or task contents.

            def send(self, status, payload, content_type="application/json; charset=utf-8"):
                body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src data:; base-uri 'none'; frame-ancestors 'none'")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_GET(self):
                port = viewer.http.server_port
                hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
                if self.headers.get("Host") not in hosts:
                    return self.send(403, {"error": "Invalid host"})
                origin = self.headers.get("Origin")
                if origin and origin not in {"http://" + h for h in hosts}:
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
                q = parse_qs(route.query)
                run_id = q.get("run_id", [""])[0]
                try:
                    if route.path == "/api/cli-maintenance":
                        return self.send(200, read_cli_maintenance())
                    if route.path == "/api/runs":
                        limit = int(q.get("limit", [50])[0])
                        offset = int(q.get("offset", [0])[0])
                        if not 1 <= limit <= 200 or offset < 0:
                            raise ValueError("Invalid history page")
                        rows = viewer.runtime.list_runs(limit=limit + 1, offset=offset)
                        return self.send(200, {
                            "runs": [{k: v for k, v in row.items() if k not in {"result", "receipt"}} for row in rows[:limit]],
                            "next_offset": offset + min(limit, len(rows)), "has_more": len(rows) > limit,
                        })
                    if route.path == "/api/snapshot":
                        return self.send(200, viewer.runtime.snapshot(run_id))
                    if route.path == "/api/events":
                        after, limit = int(q.get("after", [0])[0]), int(q.get("limit", [200])[0])
                        if after < 0 or not 1 <= limit <= 200:
                            raise ValueError("Invalid event page")
                        return self.send(200, viewer.runtime.events(run_id, after=after, limit=limit))
                    if route.path == "/api/artifact":
                        paging = {key: int(q[key][0]) for key in ("index", "offset", "limit") if key in q}
                        return self.send(200, read_artifact(viewer.runtime, run_id, q.get("name", [""])[0], **paging))
                    return self.send(404, {"error": "Not found"})
                except (ValueError, RuntimeError, KeyError, FileNotFoundError) as exc:
                    return self.send(400, {"error": str(exc)})
                except OSError as exc:
                    return self.send(500, {"error": f"{type(exc).__name__}: {exc}"})

            def do_POST(self):
                self.send(405, {"error": "Read-only viewer; send execution instructions through Codex."})

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.http.daemon_threads = True
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        return self

    def url(self, run_id=""):
        if self.http is None:
            raise RuntimeError("Viewer not started")
        return f"http://127.0.0.1:{self.http.server_port}/#token={self.token}&run={run_id}"

    def close(self):
        if self.http:
            self.http.shutdown()
            self.http.server_close()
            self.http = None
        if self.thread:
            self.thread.join(timeout=3)
