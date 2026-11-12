"""Job definitions: validation, the rclone flags they translate to, and schedules."""

from __future__ import annotations

import re
import shlex
import threading
import time
import uuid
from datetime import datetime, timedelta

from cloudhop.store import DataDir, read_json, write_json

MODES = ("copy", "sync", "move")
VERIFY = ("none", "size", "content")
AFTER_VERIFY = ("keep", "archive", "delete")
SCHEDULES = ("manual", "interval", "daily", "weekly")

DEFAULT_JOB = {
    "name": "",
    "enabled": True,
    "source": "",
    "destination": "",
    "mode": "copy",               # copy: add/update files | sync: make destination identical | move: copy then delete source
    "batch_by_folder": False,     # process each top-level folder separately (checkpointing)
    "routes": [],                 # [{"extensions": "cr2,nef", "destination": "remote:RAW"}]
    "verify": "none",             # none | size | content
    "after_verify": "keep",       # keep | archive | delete: what happens to verified source files
    "archive_to": "",             # with after_verify=archive: move verified source files here (same remote)
    "only_new": False,            # --ignore-existing
    "max_age": "",                # e.g. 7d: only files changed within this period
    "min_age": "",
    "include": "",                # one pattern per line
    "exclude": "",
    "bwlimit": "",                # e.g. 10M, or a timetable "08:00,2M 23:00,off"
    "transfers": 4,
    "checkers": 8,
    "tpslimit": 0,
    "drive_stop_on_upload_limit": False,
    "scan_sizes": True,           # measure the source first so progress and ETA are known
    "retry_minutes": 60,          # wait before retrying a failed run
    "retry_max": 0,               # 0 = don't retry
    "extra_flags": "",
    "schedule": {"type": "manual", "every_minutes": 1440, "time": "02:00", "days": [0, 1, 2, 3, 4, 5, 6]},
}

SIZE_RE = re.compile(r"^\d+(\.\d+)?[smhdwMy]?$")
# Flags that would break how cloudhop drives and reads rclone
FORBIDDEN_FLAGS = ("--config", "--rc", "--log-file", "--use-json-log", "--stats", "--dry-run")


class JobError(ValueError):
    pass


def _remote_path(value: str, field: str) -> str:
    value = (value or "").strip()
    if not value:
        raise JobError(f"{field} is required")
    if ":" not in value and not value.startswith("/"):
        raise JobError(f"{field} must be a connection path like 'mydrive:folder' (or an absolute local path)")
    return value


def split_remote(path: str) -> tuple[str, str]:
    """'remote:a/b' -> ('remote:', 'a/b')"""
    if ":" in path and not path.startswith("/"):
        remote, sub = path.split(":", 1)
        return remote + ":", sub.replace("\\", "/").strip("/")
    return "", path.replace("\\", "/").rstrip("/")


def join(base: str, sub: str) -> str:
    if not sub:
        return base
    if base.endswith(":") or base.endswith("/"):
        return base + sub
    return base + "/" + sub


def extensions(value: str) -> list[str]:
    return [e.strip().lstrip(".").lower() for e in re.split(r"[,\s]+", value or "") if e.strip()]


def validate(raw: dict) -> dict:
    job = {**DEFAULT_JOB, **{k: v for k, v in raw.items() if k in DEFAULT_JOB}}
    job["name"] = str(job["name"]).strip() or "Untitled job"
    job["source"] = _remote_path(job["source"], "Source")
    job["destination"] = _remote_path(job["destination"], "Destination")
    if job["source"].rstrip("/") == job["destination"].rstrip("/"):
        raise JobError("Source and destination are the same")
    if job["mode"] not in MODES:
        raise JobError(f"Mode must be one of {', '.join(MODES)}")
    if job["verify"] not in VERIFY:
        raise JobError(f"Verification must be one of {', '.join(VERIFY)}")
    if job["mode"] == "move" and job["verify"] != "none":
        raise JobError("Verification is not possible in move mode (the source is deleted while copying). "
                       "Use copy + verification + 'archive verified files' instead.")
    job["archive_to"] = str(job["archive_to"] or "").strip()
    if job["after_verify"] not in AFTER_VERIFY:
        raise JobError(f"After verification must be one of {', '.join(AFTER_VERIFY)}")
    if job["after_verify"] != "keep" and (job["mode"] != "copy" or job["verify"] == "none"):
        raise JobError("Archiving or deleting verified files needs mode 'copy' and a verification step")
    if job["after_verify"] != "archive":
        job["archive_to"] = ""
    elif not job["archive_to"]:
        raise JobError("Choose the archive folder for verified files")
    if job["archive_to"]:
        if split_remote(job["archive_to"])[0] != split_remote(job["source"])[0]:
            raise JobError("The archive folder must be on the same connection as the source "
                           "(moves within one connection are instant)")
    routes = []
    for r in job["routes"] or []:
        exts, dest = extensions(r.get("extensions", "")), str(r.get("destination", "")).strip()
        if not exts and not dest:
            continue
        if not exts:
            raise JobError("Each file-type route needs at least one extension")
        routes.append({"extensions": ",".join(exts), "destination": _remote_path(dest, "Route destination")})
    if routes and job["mode"] == "sync":
        raise JobError("File-type routes cannot be combined with sync mode")
    job["routes"] = routes
    for key in ("max_age", "min_age"):
        job[key] = str(job[key] or "").strip()
        if job[key] and not SIZE_RE.match(job[key]):
            raise JobError(f"{key.replace('_', ' ')} must look like 30d, 12h or 2w")
    job["bwlimit"] = str(job["bwlimit"] or "").strip()
    if job["bwlimit"] and not re.fullmatch(r"[\w.:,|\- ]+", job["bwlimit"]):
        raise JobError("Bandwidth limit looks wrong; examples: 10M, 512k, or '08:00,2M 23:00,off'")
    for key, lo, hi in (("transfers", 1, 64), ("checkers", 1, 128), ("tpslimit", 0, 1000),
                        ("retry_minutes", 1, 10080), ("retry_max", 0, 1000)):
        try:
            job[key] = max(lo, min(hi, int(job[key])))
        except (TypeError, ValueError):
            raise JobError(f"{key} must be a number")
    for key in ("enabled", "batch_by_folder", "only_new", "drive_stop_on_upload_limit", "scan_sizes"):
        job[key] = bool(job[key])
    for key in ("include", "exclude"):
        job[key] = "\n".join(ln.strip() for ln in str(job[key] or "").splitlines() if ln.strip())
    try:
        flags = shlex.split(str(job["extra_flags"] or ""))
    except ValueError as e:
        raise JobError(f"Extra flags: {e}")
    for f in flags:
        if f.split("=")[0].startswith(FORBIDDEN_FLAGS):
            raise JobError(f"Extra flag {f} is managed by cloudhop and can't be set here")
    job["extra_flags"] = str(job["extra_flags"] or "").strip()
    job["schedule"] = validate_schedule(job["schedule"])
    return job


def validate_schedule(raw: dict) -> dict:
    s = {**DEFAULT_JOB["schedule"], **(raw or {})}
    if s["type"] not in SCHEDULES:
        raise JobError("Unknown schedule type")
    try:
        s["every_minutes"] = max(5, int(s["every_minutes"]))
    except (TypeError, ValueError):
        raise JobError("Interval must be a number of minutes")
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(s["time"])):
        raise JobError("Time must be HH:MM (24-hour clock)")
    s["days"] = sorted({int(d) for d in s["days"] if 0 <= int(d) <= 6})
    if s["type"] == "weekly" and not s["days"]:
        raise JobError("Pick at least one weekday")
    return {k: s[k] for k in DEFAULT_JOB["schedule"]}


def next_run(schedule: dict, after: float) -> float | None:
    """Next start time strictly after `after` (epoch seconds), local time."""
    kind = schedule["type"]
    if kind == "manual":
        return None
    if kind == "interval":
        return after + schedule["every_minutes"] * 60
    hh, mm = map(int, schedule["time"].split(":"))
    base = datetime.fromtimestamp(after)
    candidate = base.replace(hour=hh, minute=mm, second=0, microsecond=0)
    for _ in range(8):
        if candidate.timestamp() > after and (kind == "daily" or candidate.weekday() in schedule["days"]):
            return candidate.timestamp()
        candidate += timedelta(days=1)
    return None


def transfer_flags(job: dict) -> list[str]:
    flags = ["--transfers", str(job["transfers"]), "--checkers", str(job["checkers"])]
    if job["tpslimit"]:
        flags += ["--tpslimit", str(job["tpslimit"])]
    if job["bwlimit"]:
        flags += ["--bwlimit", job["bwlimit"]]
    if job["drive_stop_on_upload_limit"]:
        flags.append("--drive-stop-on-upload-limit")
    flags += shlex.split(job["extra_flags"])
    return flags


class JobStore:
    def __init__(self, data: DataDir):
        self.data = data
        self._lock = threading.Lock()
        self._jobs: dict[str, dict] = {j["id"]: j for j in read_json(data.jobs_file, [])}

    def all(self) -> list[dict]:
        return sorted(self._jobs.values(), key=lambda j: j.get("created", 0))

    def get(self, job_id: str) -> dict | None:
        return self._jobs.get(job_id)

    def save(self, raw: dict) -> dict:
        job = validate(raw)
        with self._lock:
            existing = self._jobs.get(raw.get("id") or "")
            job["id"] = existing["id"] if existing else "j" + uuid.uuid4().hex[:10]
            job["created"] = existing["created"] if existing else time.time()
            schedule_changed = not existing or existing.get("schedule") != job["schedule"]
            job["next_run"] = (next_run(job["schedule"], time.time()) if schedule_changed
                               else existing.get("next_run"))
            self._jobs[job["id"]] = job
            self._flush()
        return job

    def set_next_run(self, job_id: str, when: float | None) -> None:
        with self._lock:
            if job_id in self._jobs:
                self._jobs[job_id]["next_run"] = when
                self._flush()

    def delete(self, job_id: str) -> None:
        with self._lock:
            self._jobs.pop(job_id, None)
            self._flush()

    def _flush(self) -> None:
        write_json(self.data.jobs_file, list(self._jobs.values()))
