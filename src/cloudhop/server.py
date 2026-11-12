"""The embedded web server: JSON API + the static single-page UI (standard library only)."""

from __future__ import annotations

import configparser
import hmac
import json
import mimetypes
import re
import subprocess
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from cloudhop import __version__
from cloudhop.jobs import JobError, JobStore, next_run, validate_schedule
from cloudhop.rclone import NO_WINDOW, Rclone, RcloneError, SetupSession
from cloudhop.runner import RunManager
from cloudhop.store import Settings

STATIC = Path(__file__).parent / "static"


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class App:
    """Holds the shared services the request handler needs."""

    def __init__(self, settings: Settings, rclone: Rclone, jobs: JobStore, runs: RunManager, token: str):
        self.settings = settings
        self.rclone = rclone
        self.jobs = jobs
        self.runs = runs
        self.token = token

    # ------------------------------------------------------------------ helpers

    def detected_config(self) -> dict | None:
        """An existing rclone config (e.g. from using rclone before) that cloudhop isn't using yet."""
        path = self.rclone.binary()
        if not path:
            return None
        try:
            out = subprocess.run([path, "config", "file"], capture_output=True, text=True, timeout=20,
                                 creationflags=NO_WINDOW).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        found = Path(out.strip().splitlines()[-1]) if out.strip() else None
        if not found or not found.exists() or found.resolve() == self.settings.rclone_config.resolve():
            return None
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(found, encoding="utf-8")
            names = parser.sections()
        except configparser.Error:
            names = []
        return {"path": str(found), "remotes": names}

    def jobs_overview(self) -> list[dict]:
        history = self.runs.history(limit=500)
        out = []
        for job in self.jobs.all():
            active = self.runs.active_for_job(job["id"])
            last = next((r for r in history if r["job_id"] == job["id"]), None)
            out.append({**job, "active_run": active.summary() if active else None, "last_run": last})
        return out

    # ------------------------------------------------------------------ routing

    def handle(self, method: str, path: str, query: dict, body: dict):
        parts = [unquote(p) for p in path.strip("/").split("/")][1:]  # drop "api"
        route = (method, *parts)

        match route:
            case ("GET", "state"):
                return {"version": __version__, "rclone": self.rclone.info(),
                        "settings": self.settings.all(), "detected_config": self.detected_config()}
            case ("POST", "settings"):
                result = self.settings.update(body)
                self.rclone._providers = None
                return result
            case ("POST", "rclone", "install"):
                if self.rclone.install_status["state"] != "running":
                    threading.Thread(target=self._install, daemon=True).start()
                return self.rclone.info()
            case ("GET", "providers"):
                return self.rclone.providers()
            case ("GET", "remotes"):
                return self.rclone.remotes()
            case ("POST", "remotes"):
                name, rtype = str(body.get("name", "")).strip(), str(body.get("type", "")).strip()
                if name in {r["name"] for r in self.rclone.remotes()}:
                    raise ApiError(400, f"A connection called '{name}' already exists")
                params = {k: str(v) for k, v in (body.get("params") or {}).items()}
                return SetupSession(self.rclone, name, rtype, params).to_dict()
            case ("GET", "remotes", "setup", sid):
                return self._session(sid).to_dict()
            case ("POST", "remotes", "setup", sid, "answer"):
                session = self._session(sid)
                session.answer(str(body.get("result", "")))
                return session.to_dict()
            case ("POST", "remotes", "setup", sid, "cancel"):
                session = self._session(sid)
                session.cancel()
                return session.to_dict()
            case ("POST", "remotes", name, "test"):
                return self.rclone.test_remote(name)
            case ("DELETE", "remotes", name):
                self.rclone.delete_remote(name)
                return {"ok": True}
            case ("GET", "browse"):
                return self.rclone.browse(query.get("path", [""])[0])
            case ("GET", "jobs"):
                return self.jobs_overview()
            case ("POST", "jobs"):
                return self.jobs.save(body)
            case ("POST", "jobs", "preview-schedule"):
                when, upcoming = time.time(), []
                sched = validate_schedule(body)
                for _ in range(3):
                    when = next_run(sched, when)
                    if not when:
                        break
                    upcoming.append(when)
                return upcoming
            case ("DELETE", "jobs", job_id):
                if self.runs.active_for_job(job_id):
                    raise ApiError(400, "Stop the job's active run before deleting it")
                self.jobs.delete(job_id)
                return {"ok": True}
            case ("POST", "jobs", job_id, "run"):
                try:
                    return self.runs.start(job_id).summary()
                except KeyError as e:
                    raise ApiError(404, str(e))
            case ("GET", "runs"):
                job = query.get("job", [None])[0]
                limit = int(query.get("limit", ["50"])[0])
                return self.runs.history(job, limit)
            case ("GET", "runs", run_id):
                run = self.runs.get(run_id)
                if not run:
                    raise ApiError(404, "No such run")
                return run.detail()
            case ("POST", "runs", run_id, "stop"):
                self.runs.stop(run_id)
                return {"ok": True}
            case ("POST", "runs", run_id, "resume"):
                return self.runs.resume(run_id).summary()
        raise ApiError(404, f"Unknown API endpoint {method} {path}")

    def _session(self, sid: str) -> SetupSession:
        session = SetupSession.sessions.get(sid)
        if not session:
            raise ApiError(404, "Setup session expired; start again")
        return session

    def _install(self) -> None:
        try:
            self.rclone.install()
        except Exception:
            pass  # status is reported through install_status


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"cloudhop/{__version__}"

        def log_message(self, *args):  # keep the terminal quiet
            pass

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, data) -> None:
            self._send(status, json.dumps(data).encode(), "application/json")

        def _api(self, method: str) -> None:
            url = urlparse(self.path)
            supplied = self.headers.get("X-Cloudhop-Token", "")
            if not hmac.compare_digest(supplied, app.token):
                return self._json(401, {"error": "Missing or wrong access token. Open the address "
                                                 "cloudhop printed when it started."})
            body = {}
            if method in ("POST", "PUT"):
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    try:
                        body = json.loads(self.rfile.read(length))
                    except ValueError:
                        return self._json(400, {"error": "Invalid JSON"})
            try:
                result = app.handle(method, url.path, parse_qs(url.query), body)
                self._json(200, result)
            except ApiError as e:
                self._json(e.status, {"error": str(e)})
            except (JobError, RcloneError, ValueError) as e:
                self._json(400, {"error": str(e)})
            except KeyError as e:
                self._json(404, {"error": str(e).strip("'")})
            except subprocess.TimeoutExpired:
                self._json(504, {"error": "rclone took too long to answer"})
            except Exception as e:
                traceback.print_exc()
                self._json(500, {"error": f"Internal error: {e}"})

        def _static(self) -> None:
            name = urlparse(self.path).path.lstrip("/") or "index.html"
            if not re.fullmatch(r"[\w\-]+\.(html|js|css|svg|png|ico)", name):
                name = "index.html"  # client-side routes
            path = STATIC / name
            if not path.is_file():
                return self._send(404, b"Not found", "text/plain")
            ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            self._send(200, path.read_bytes(), ctype)

        def do_GET(self):
            if self.path.startswith("/api/"):
                return self._api("GET")
            self._static()

        def do_POST(self):
            if self.path.startswith("/api/"):
                return self._api("POST")
            self._send(405, b"", "text/plain")

        def do_DELETE(self):
            if self.path.startswith("/api/"):
                return self._api("DELETE")
            self._send(405, b"", "text/plain")

    return Handler


def serve(app: App, host: str, port: int) -> ThreadingHTTPServer:
    """Bind to the requested port, or the next free one."""
    last_error = None
    for candidate in range(port, port + 20):
        try:
            server = ThreadingHTTPServer((host, candidate), make_handler(app))
            server.daemon_threads = True
            return server
        except OSError as e:
            last_error = e
    raise SystemExit(f"Could not open a port between {port} and {port + 19}: {last_error}")
