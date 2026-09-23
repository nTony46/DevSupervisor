"""Local dashboard: read-only execution state, separately versioned UI settings."""

import json
import secrets
import sqlite3
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .reader import DEFAULT_ACTIVITY_LIMIT, Reader, StateUnavailable
from .settings import Settings, Conflict

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
        self.settings = Settings(reader.db_path)
        self.edit_token = secrets.token_urlsafe(32)


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "DevSupervisorDashboard"
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):        # noqa: A002 - stdlib signature
        """Quiet by default; the console shows the URL, not a request log."""

    def do_POST(self):
        route = urlparse(self.path).path
        if route not in ('/api/profiles', '/api/workflow-name'):
            return self._refuse()
        origin = self.headers.get('Origin')
        expected_origin = f'http://{self.headers.get("Host", "")}'
        host = self.headers.get('Host', '')
        allowed_hosts = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
        if (host not in allowed_hosts or (origin and origin != expected_origin)
                or self.headers.get('Sec-Fetch-Site') == 'cross-site'
                or not secrets.compare_digest(self.headers.get('X-Dashboard-Token', ''), self.server.edit_token)):
            self.close_connection = True
            return self._send_json({'error': 'Reload this dashboard before saving.'}, status=403)
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length < 1 or length > 65536 or self.headers.get_content_type() != 'application/json':
                raise ValueError('Expected a JSON request under 64 KB')
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError('Expected an object')
            if route == '/api/profiles':
                result = self.server.settings.profile(data)
            else:
                project = self.server.reader.resolve_project(data.get('project'))
                workflow = data.get('workflow')
                if not project or not workflow:
                    raise ValueError('Select a workflow first')
                if workflow != 'unscoped':
                    self.server.reader.workflow(project['id'], workflow)
                result = self.server.settings.rename(project['id'] + ':' + workflow, data)
        except Conflict as exc:
            return self._send_json({'error': str(exc)}, status=409)
        except (ValueError, TypeError, StateUnavailable) as exc:
            self.close_connection = True
            return self._send_json({'error': str(exc)}, status=400)
        except (sqlite3.Error, OSError):
            return self._send_json({'error': 'Could not save settings. Check available disk space and try again.'}, status=500)
        return self._send_json(result)

    def do_PUT(self):
        self._refuse()

    do_PATCH = do_DELETE = do_PUT

    def _refuse(self):
        self.close_connection = True
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
            "/api/workflows": self._api_workflows,
            "/api/profiles": self._api_profiles,
            "/api/settings": lambda query: {"token": self.server.edit_token},
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

    def _api_workflows(self, query):
        labels = self.server.settings.all('workflow')
        rows = self.server.reader.workflows()
        for row in rows:
            label = labels.get(row['project_id'] + ':' + row['id'], {})
            row['original_title'] = row['title']
            row['title'] = label.get('name') or row['title']
            row['name_revision'] = label.get('revision', 0)
        return {'workflows': rows}

    def _api_profiles(self, query):
        return {'profiles': list(self.server.settings.all('profile').values())}

    def _api_state(self, query):
        state = self.server.reader.state(_first(query, "project"), _first(query, "workflow"))
        saved = self.server.settings.all('profile')
        profiles = []
        for preset in state.get('agent_library', []):
            identifier = 'preset-' + preset['role']
            profiles.append({**preset, 'id': identifier, 'revision': 0, **saved.pop(identifier, {})})
        state['agent_library'] = profiles + list(saved.values())
        if state.get('project'):
            workflow = _first(query, 'workflow') or (state.get('goal') or {}).get('id') or 'unscoped'
            label = self.server.settings.all('workflow').get(state['project']['id'] + ':' + workflow, {})
            state['workflow_id'] = workflow
            state['name_revision'] = label.get('revision', 0)
            state['workflow_name'] = label.get('name') or (state.get('goal') or {}).get('title') or state['project']['name']
        return state

    def _api_activity(self, query):
        return self.server.reader.activity(
            _first(query, "project"),
            limit=_first(query, "limit") or DEFAULT_ACTIVITY_LIMIT,
            before=_first(query, "before"),
            kind=_first(query, "kind"),
            workflow=_first(query, "workflow"),
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
