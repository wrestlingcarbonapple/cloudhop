"""Everything that talks to the rclone binary: finding/downloading it, connections
("remotes"), browsing, and the interactive setup flow including browser logins."""

from __future__ import annotations

import hashlib
import io
import json
import platform
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import zipfile
from pathlib import Path

from cloudhop import __version__
from cloudhop.store import DataDir, Settings

DOWNLOADS = "https://downloads.rclone.org"
USER_AGENT = f"cloudhop/{__version__}"
EXE = "rclone.exe" if sys.platform == "win32" else "rclone"
# Hide console windows for child processes on Windows
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class RcloneError(Exception):
    pass


def _http_get(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _platform_tag() -> str:
    os_name = {"win32": "windows", "darwin": "osx"}.get(sys.platform, "linux")
    machine = platform.machine().lower()
    arch = {
        "x86_64": "amd64", "amd64": "amd64",
        "aarch64": "arm64", "arm64": "arm64",
        "armv7l": "arm-v7", "armv6l": "arm",
        "i386": "386", "i686": "386", "x86": "386",
    }.get(machine)
    if not arch:
        raise RcloneError(f"No rclone download for this machine type ({machine}). Install rclone yourself and set its path in Settings.")
    return f"{os_name}-{arch}"


class Rclone:
    def __init__(self, data: DataDir, settings: Settings):
        self.data = data
        self.settings = settings
        self._version_cache: tuple[str, str] | None = None  # (path, version)
        self._providers: list | None = None
        self.install_status = {"state": "idle", "message": ""}

    # ---------------------------------------------------------------- binary

    @property
    def managed_path(self) -> Path:
        return self.data.bin / EXE

    def binary(self) -> str | None:
        configured = self.settings.get("rclone_path")
        if configured:
            return configured if Path(configured).expanduser().exists() else None
        if self.managed_path.exists():
            return str(self.managed_path)
        return shutil.which("rclone")

    def require(self) -> str:
        path = self.binary()
        if not path:
            raise RcloneError("rclone is not installed yet. Open Settings and click 'Download rclone'.")
        return path

    def version(self) -> str | None:
        path = self.binary()
        if not path:
            return None
        if self._version_cache and self._version_cache[0] == path:
            return self._version_cache[1]
        try:
            out = subprocess.run([path, "version"], capture_output=True, text=True, timeout=20,
                                 creationflags=NO_WINDOW).stdout
            ver = out.split()[1] if out.startswith("rclone") else None
        except (OSError, subprocess.SubprocessError, IndexError):
            ver = None
        self._version_cache = (path, ver)
        return ver

    def latest_version(self) -> str | None:
        try:
            return _http_get(f"{DOWNLOADS}/version.txt", timeout=10).decode().split()[-1]
        except Exception:
            return None

    def info(self) -> dict:
        path = self.binary()
        return {
            "path": path,
            "version": self.version(),
            "managed": bool(path) and Path(path) == self.managed_path,
            "install": self.install_status,
            "config_file": str(self.settings.rclone_config),
        }

    def install(self) -> str:
        """Download the latest official rclone release into the data dir, verifying its SHA-256."""
        self.install_status = {"state": "running", "message": "Checking latest version…"}
        try:
            version = self.latest_version()
            if not version:
                raise RcloneError("Could not reach downloads.rclone.org")
            name = f"rclone-{version}-{_platform_tag()}"
            sums = _http_get(f"{DOWNLOADS}/{version}/SHA256SUMS").decode()
            expected = next((line.split()[0] for line in sums.splitlines()
                             if line.strip().endswith(f"{name}.zip")), None)
            if not expected:
                raise RcloneError(f"No checksum published for {name}.zip")
            self.install_status = {"state": "running", "message": f"Downloading rclone {version}…"}
            blob = _http_get(f"{DOWNLOADS}/{version}/{name}.zip", timeout=300)
            if hashlib.sha256(blob).hexdigest() != expected:
                raise RcloneError("Downloaded rclone failed its checksum check; not installing it.")
            with zipfile.ZipFile(io.BytesIO(blob)) as z:
                member = next(m for m in z.namelist() if m.endswith("/" + EXE))
                self.data.bin.mkdir(parents=True, exist_ok=True)
                tmp = self.managed_path.with_suffix(".download")
                tmp.write_bytes(z.read(member))
                tmp.chmod(0o755)
                tmp.replace(self.managed_path)
            self._version_cache = None
            self.install_status = {"state": "done", "message": f"Installed rclone {version}"}
            return version
        except Exception as e:
            self.install_status = {"state": "error", "message": str(e)}
            raise

    # ---------------------------------------------------------------- running

    def base_cmd(self) -> list[str]:
        cmd = [self.require(), "--config", str(self.settings.rclone_config)]
        if self.settings.get("disable_http2"):
            cmd.append("--disable-http2")
        return cmd

    def run(self, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
        return subprocess.run(self.base_cmd() + list(args), capture_output=True, text=True,
                              timeout=timeout, creationflags=NO_WINDOW)

    def run_json(self, *args: str, timeout: int = 120):
        proc = self.run(*args, timeout=timeout)
        if proc.returncode != 0:
            raise RcloneError(_last_error(proc.stderr) or f"rclone exited with code {proc.returncode}")
        return json.loads(proc.stdout or "null")

    # ---------------------------------------------------------------- remotes

    def providers(self) -> list:
        """Backend types with their options, trimmed for the UI."""
        if self._providers is None:
            raw = self.run_json("config", "providers")
            out = []
            for p in raw:
                if p.get("Hide"):
                    continue
                out.append({
                    "name": p["Name"],
                    "description": p.get("Description", ""),
                    "options": [{
                        "name": o["Name"],
                        "help": o.get("Help", ""),
                        "default": o.get("DefaultStr", ""),
                        "required": bool(o.get("Required")),
                        "password": bool(o.get("IsPassword")),
                        "advanced": bool(o.get("Advanced")),
                        "type": o.get("Type", "string"),
                        "examples": [{"value": e.get("Value", ""), "help": e.get("Help", "")}
                                     for e in (o.get("Examples") or [])],
                        "exclusive": bool(o.get("Exclusive")),
                        "provider": o.get("Provider", ""),
                    } for o in p.get("Options", []) if not o.get("Hide")],
                })
            self._providers = sorted(out, key=lambda p: p["name"])
        return self._providers

    def remotes(self) -> list[dict]:
        """Names and types only: the config dump contains secrets that never leave the server."""
        if not self.settings.rclone_config.exists():
            return []
        dump = self.run_json("config", "dump")
        return [{"name": name, "type": conf.get("type", "?")} for name, conf in sorted(dump.items())]

    def delete_remote(self, name: str) -> None:
        _check_name(name)
        proc = self.run("config", "delete", name)
        if proc.returncode != 0:
            raise RcloneError(_last_error(proc.stderr))

    def test_remote(self, name: str) -> dict:
        _check_name(name)
        result = {"ok": False, "about": None, "entries": [], "error": None}
        try:
            entries = self.run_json("lsjson", f"{name}:", "--max-depth", "1", "--no-modtime",
                                    "--no-mimetype", timeout=90)
            result["entries"] = [{"name": e["Name"], "dir": e["IsDir"], "size": e.get("Size", -1)}
                                 for e in entries[:200]]
            result["count"] = len(entries)
            result["ok"] = True
        except (RcloneError, subprocess.TimeoutExpired) as e:
            result["error"] = str(e) if not isinstance(e, subprocess.TimeoutExpired) else "Timed out after 90 s"
            return result
        try:
            result["about"] = self.run_json("about", f"{name}:", "--json", timeout=60)
        except Exception:
            pass  # not every backend reports quota
        return result

    def about(self, path: str) -> dict:
        """Quota of the connection a path is on (not every backend reports it)."""
        remote = path.split(":", 1)[0] + ":" if ":" in path else path
        return self.run_json("about", remote, "--json", timeout=60)

    def browse(self, path: str) -> list[dict]:
        if ":" not in path:
            raise RcloneError("Path must look like remote:folder")
        entries = self.run_json("lsjson", path, "--dirs-only", "--max-depth", "1", "--no-modtime",
                                "--no-mimetype", timeout=90)
        return sorted(({"name": e["Name"]} for e in entries), key=lambda e: e["name"].lower())

    def hashes(self, fs: str) -> list[str]:
        """Hash types the backend provides natively (cached per remote)."""
        remote = fs.split(":", 1)[0] + ":"
        cache = getattr(self, "_hash_cache", {})
        self._hash_cache = cache
        if remote not in cache:
            try:
                cache[remote] = self.run_json("backend", "features", remote, timeout=60).get("Hashes") or []
            except Exception:
                cache[remote] = []
        return cache[remote]


def _check_name(name: str) -> None:
    if not re.fullmatch(r"[\w.\- ]+", name or "") or name.startswith(("-", " ")):
        raise RcloneError("Connection names may contain letters, numbers, spaces, '.', '-' and '_'")


def _last_error(stderr: str) -> str:
    lines = [ln for ln in (stderr or "").splitlines() if ln.strip()]
    for ln in reversed(lines):
        if "ERROR" in ln or "Failed" in ln or "CRITICAL" in ln:
            return re.sub(r"^\d{4}/\d\d/\d\d \d\d:\d\d:\d\d ", "", ln)
    return lines[-1] if lines else ""


class SetupSession:
    """Drives rclone's non-interactive config state machine for a new connection.

    rclone answers each step with a question (an "Option") or nothing when done.
    Browser logins (OAuth) are handled here: we answer "not local" and then run the
    exact `rclone authorize ...` command rclone asks for, surfacing its login link.
    """

    sessions: dict[str, SetupSession] = {}

    def __init__(self, rclone: Rclone, name: str, rtype: str, params: dict):
        _check_name(name)
        self.id = uuid.uuid4().hex
        self.rclone = rclone
        self.name = name
        self.rtype = rtype
        self.status = "working"     # working | question | authorizing | done | error
        self.question = None
        self.state = ""
        self.error = None
        self.auth_url = None
        self._auth_proc = None
        self.updated = time.time()
        SetupSession.sessions[self.id] = self
        args = ["config", "create", name, rtype]
        args += [f"{k}={v}" for k, v in params.items() if v not in ("", None)]
        args += ["--non-interactive", "--obscure"]
        self._step(args)

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "status": self.status, "question": self.question,
                "error": self.error, "auth_url": self.auth_url}

    def answer(self, result: str) -> None:
        if self.status != "question":
            raise RcloneError("This setup step is not waiting for an answer")
        self._step(["config", "update", self.name, "--continue", "--state", self.state,
                    "--result", str(result), "--non-interactive", "--obscure"])

    def cancel(self) -> None:
        if self._auth_proc and self._auth_proc.poll() is None:
            self._auth_proc.terminate()
        self.status, self.error = "error", "Cancelled"

    def _step(self, args: list[str]) -> None:
        self.updated = time.time()
        proc = self.rclone.run(*args, timeout=120)
        try:
            reply = json.loads(proc.stdout)
        except ValueError:
            self.status = "error"
            self.error = _last_error(proc.stderr) or "rclone did not answer"
            return
        if reply.get("Error"):
            self.error = reply["Error"]  # shown next to the repeated question
        opt = reply.get("Option")
        if not reply.get("State") or not opt:
            self.status, self.question = ("error" if proc.returncode else "done"), None
            if proc.returncode:
                self.error = self.error or _last_error(proc.stderr)
            return
        self.state = reply["State"]
        if opt.get("Name") == "config_is_local":
            # Always use the explicit authorize flow so we can show the login link in the UI
            return self._step(["config", "update", self.name, "--continue", "--state", self.state,
                               "--result", "false", "--non-interactive", "--obscure"])
        if opt.get("Name") == "config_token":
            command = re.search(r"rclone authorize .*", opt.get("Help", ""))
            if command:
                return self._authorize(shlex.split(command.group(0))[1:])
        self.status = "question"
        self.question = {
            "name": opt.get("Name"),
            "help": opt.get("Help", ""),
            "default": opt.get("DefaultStr", opt.get("Default", "")),
            "required": bool(opt.get("Required")),
            "password": bool(opt.get("IsPassword")),
            "exclusive": bool(opt.get("Exclusive")),
            "examples": [{"value": str(e.get("Value", "")), "help": e.get("Help", "")}
                         for e in (opt.get("Examples") or [])],
            "type": opt.get("Type", "string"),
        }

    def _authorize(self, args: list[str]) -> None:
        self.status = "authorizing"
        self.question = None
        cmd = self.rclone.base_cmd() + args
        self._auth_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           text=True, creationflags=NO_WINDOW)
        threading.Thread(target=self._watch_authorize, daemon=True).start()

    def _watch_authorize(self) -> None:
        proc = self._auth_proc
        stderr_lines = []

        def read_stderr():
            for line in proc.stderr:
                stderr_lines.append(line)
                m = re.search(r"(http://127\.0\.0\.1:\d+/auth\?state=\S+)", line)
                if m:
                    self.auth_url = m.group(1)

        t = threading.Thread(target=read_stderr, daemon=True)
        t.start()
        out = proc.stdout.read()
        proc.wait()
        t.join(timeout=2)
        if self.status != "authorizing":
            return  # cancelled
        token = re.search(r"--->\s*(.*?)\s*<---End paste", out, re.S)
        if proc.returncode != 0 or not token:
            self.status = "error"
            self.error = _last_error("".join(stderr_lines)) or "Login did not complete"
            return
        self.status = "working"
        self._step(["config", "update", self.name, "--continue", "--state", self.state,
                    "--result", token.group(1).strip(), "--non-interactive", "--obscure"])
