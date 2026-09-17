"""The localhost dashboard server.

Small on purpose: a read-only HTTP surface over `reader.Reader`, plus three
static files. It binds to the loopback interface and refuses every method that
is not a read, so "the dashboard cannot change supervisor state" is enforced at
the transport as well as by the read-only database connection.
"""

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .reader import DEFAULT_ACTIVITY_LIMIT, Reader, StateUnavailable

HOST = "127.0.0.1"          # loopback only; deliberately not configurable
DEFAULT_PORT = 8765

STATIC_DIR = Path(__file__).with_name("static")
# An explicit map, so no request path can ever be turned into a filesystem read.
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, reader):
        super().__init__(address, DashboardHandler)
        self.reader = reader


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "DevSupervisorDashboard"
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):        # noqa: A002 - stdlib signature
        """Quiet by default; the console shows the URL, not a request log."""

    # Only reads exist. Everything else is refused before routing, so a mutating
    # request cannot reach the database layer even by accident.
    def do_POST(self):
        self._refuse()

    do_PUT = do_PATCH = do_DELETE = do_POST

    def _refuse(self):
        self._send_json({"error": "the dashboard is read-only"},
                        status=HTTPStatus.METHOD_NOT_ALLOWED)

    def do_HEAD(self):
        self.do_GET(body=False)

    def do_GET(self, body=True):
        parsed = urlparse(self.path)
        route = parsed.path
        query = parse_qs(parsed.query)
        if route in STATIC_FILES:
            return self._send_static(route, body)
        handler = {
            "/api/projects": self._api_projects,
            "/api/state": self._api_state,
            "/api/activity": self._api_activity,
            "/api/job": self._api_job,
        }.get(route)
        if handler is None:
            return self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND,
                                   body=body)
        try:
            # No lock here. `Reader` guards its own connection, and a lock at
            # this level would hold every endpoint behind one request's git
            # subprocesses -- six invocations at a 30s timeout each.
            payload = handler(query)
        except StateUnavailable as exc:
            return self._send_json({"error": str(exc)}, status=HTTPStatus.NOT_FOUND, body=body)
        except Exception as exc:                              # pragma: no cover - defensive
            return self._send_json({"error": f"{type(exc).__name__}: {exc}"},
                                   status=HTTPStatus.INTERNAL_SERVER_ERROR, body=body)
        return self._send_json(payload, body=body)

    # --- routes -----------------------------------------------------------

    def _api_projects(self, query):
        return {"projects": self.server.reader.projects()}

    def _api_state(self, query):
        return self.server.reader.state(_first(query, "project"))

    def _api_activity(self, query):
        return self.server.reader.activity(
            _first(query, "project"),
            limit=_first(query, "limit") or DEFAULT_ACTIVITY_LIMIT,
            before=_first(query, "before"),
            kind=_first(query, "kind"),
        )

    def _api_job(self, query):
        job_id = _first(query, "id")
        if not job_id:
            raise StateUnavailable("missing job id")
        return self.server.reader.detail(job_id)

    # --- responses --------------------------------------------------------

    def _send_static(self, route, body=True):
        name, content_type = STATIC_FILES[route]
        try:
            payload = (STATIC_DIR / name).read_bytes()
        except OSError:                                       # pragma: no cover - packaging
            return self._send_json({"error": f"missing asset {name}"},
                                   status=HTTPStatus.INTERNAL_SERVER_ERROR)
        self._respond(payload, content_type, HTTPStatus.OK, body)

    def _send_json(self, payload, status=HTTPStatus.OK, body=True):
        encoded = json.dumps(payload, default=str).encode("utf-8")
        self._respond(encoded, "application/json; charset=utf-8", status, body)

    def _respond(self, payload, content_type, status, body=True):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        # The dashboard reads local operational state; no page should be able to
        # embed it, and nothing here is meant to be fetched cross-origin.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        if body:
            self.wfile.write(payload)


def _first(query, key):
    values = query.get(key)
    return values[0] if values else None


def build_server(db_path=None, port=DEFAULT_PORT, host=HOST):
    """Bind the server. Raises StateUnavailable if durable state cannot be read."""
    return DashboardServer((host, port), Reader(db_path))


def serve(db_path=None, port=DEFAULT_PORT, on_ready=None):
    """Run until interrupted. Loopback only."""
    server = build_server(db_path=db_path, port=port)
    url = f"http://{HOST}:{server.server_address[1]}"
    if on_ready:
        on_ready(url)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        server.reader.close()
    return url
