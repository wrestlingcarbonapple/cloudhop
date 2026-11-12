"""Executing jobs: runs, their units (top-level folders), live stats, retries and the scheduler.

A run processes one or more *units*. Without "batch by folder" there is a single unit
(the whole source). With it, each top-level folder is a unit, plus one for loose files
at the source root, so progress is checkpointed folder by folder.

Per unit:  transfer (main destination + file-type routes)  ->  verify  ->  archive/delete.
A full storage account pauses the run until the user frees space and resumes it.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from cloudhop.jobs import JobStore, extensions, join, next_run, split_remote, transfer_flags
from cloudhop.rclone import NO_WINDOW, Rclone, RcloneError
from cloudhop.store import DataDir, Settings, read_json, write_json

ROOT_UNIT = "(files in the top folder)"
WHOLE_UNIT = "(everything)"
DRIVE_DAILY_LIMIT = 750 * 10**9
ACTIVE = ("queued", "running")
LIVE_STATUSES = ("queued", "running", "waiting_retry", "paused")
# Storage-full errors from the common backends: these pause the run and ask the user for space
QUOTA_RE = re.compile(r"quotaLimitReached|storageQuotaExceeded|insufficient ?storage|quota (exceeded|limit)|"
                      r"not enough (space|storage)|no space left|disk full|InsufficientStorage|507 Insufficient", re.I)
FATAL_EXIT = (7, 8)  # rclone: fatal error (e.g. Drive upload limit) / --max-transfer reached
LOG_TAIL_BYTES = 24_000


class StopRun(Exception):
    pass


class Cancelled(Exception):
    pass


class NeedsAttention(Exception):
    """Something only the user can fix (e.g. a full storage account): pause and wait."""


def filter_flags(job: dict, route: dict | None = None, split: bool = True) -> list[str]:
    """rclone --filter rules for the main destination (route=None) or one file-type route.

    split=False gives the union of everything the job selects (used for size scans).
    """
    rules = ["- " + p for p in job["exclude"].splitlines()]
    includes = job["include"].splitlines()
    if route:
        rules += ["+ *.{%s}" % route["extensions"], "- **"]
    else:
        routed = sorted({e for r in job["routes"] for e in extensions(r["extensions"])})
        if routed and split:
            rules.append("- *.{%s}" % ",".join(routed))
        rules += ["+ " + p for p in includes]
        if includes:
            rules.append("- **")
    flags = ["--ignore-case"]
    for rule in rules:
        flags += ["--filter", rule]
    if job["max_age"]:
        flags += ["--max-age", job["max_age"]]
    if job["min_age"]:
        flags += ["--min-age", job["min_age"]]
    return flags


class KeepAwake:
    """Ask the OS not to sleep while transfers run (best effort, no extra software)."""

    def __init__(self):
        self._proc = None
        self._active = False

    def set(self, active: bool) -> None:
        if active == self._active:
            return
        self._active = active
        if sys.platform == "win32":
            import ctypes
            ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
            flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if active else 0)
            try:
                ctypes.windll.kernel32.SetThreadExecutionState(flags)
            except Exception:
                pass
            return
        if active:
            cmd = (["caffeinate", "-i", "-w", str(os.getpid())] if sys.platform == "darwin" else
                   ["systemd-inhibit", "--what=sleep:idle", "--who=cloudhop",
                    "--why=File transfer in progress", "sleep", "infinity"])
            try:
                self._proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                self._proc = None
        elif self._proc:
            self._proc.terminate()
            self._proc = None


class Run:
    """One execution of a job (may span several attempts when retrying)."""

    def __init__(self, folder: Path, data: dict):
        self.folder = folder
        self.d = data
        self.lock = threading.RLock()
        self.live = None              # latest rclone stats while a step runs
        self.proc = None
        self.cancel_event = threading.Event()
        self._last_save = 0.0
        self._attempt_started = None

    # -- persistence
    @classmethod
    def create(cls, runs_dir: Path, job: dict, trigger: str) -> Run:
        run_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        data = {
            "id": run_id, "job_id": job["id"], "job": job, "trigger": trigger,
            "status": "queued", "created": time.time(), "started": None, "ended": None,
            "attempt": 0, "retry_at": None, "error": None, "units": None,
            "transferred": 0, "verified": 0, "max_speed": 0, "active_seconds": 0, "days": {},
        }
        run = cls(runs_dir / run_id, data)
        run.folder.mkdir(parents=True)
        run.save(force=True)
        return run

    @classmethod
    def load(cls, folder: Path) -> Run | None:
        data = read_json(folder / "run.json", None)
        return cls(folder, data) if data else None

    @property
    def id(self) -> str:
        return self.d["id"]

    def save(self, force: bool = False) -> None:
        now = time.time()
        if force or now - self._last_save > 5:
            with self.lock:
                write_json(self.folder / "run.json", self.d)
            self._last_save = now

    def log(self, text: str) -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(self.folder / "log.txt", "a", encoding="utf-8") as f:
            f.write(f"{stamp}  {text}\n")

    def log_tail(self) -> list[str]:
        try:
            with open(self.folder / "log.txt", "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - LOG_TAIL_BYTES))
                return f.read().decode("utf-8", "replace").splitlines()[-40:]
        except FileNotFoundError:
            return []

    # -- state helpers
    def set_unit(self, unit: dict, **changes) -> None:
        with self.lock:
            unit.update(changes, updated=time.time())
        self.save()

    def add_transferred(self, unit: dict, delta: int) -> None:
        if delta <= 0:
            return
        day = time.strftime("%Y-%m-%d")
        with self.lock:
            unit["transferred"] = unit.get("transferred", 0) + delta
            self.d["transferred"] += delta
            self.d["days"][day] = self.d["days"].get(day, 0) + delta

    def active_seconds(self) -> float:
        extra = time.time() - self._attempt_started if self._attempt_started else 0
        return self.d["active_seconds"] + extra

    def summary(self) -> dict:
        with self.lock:
            d = self.d
            units = d["units"] or []
            return {
                "id": d["id"], "job_id": d["job_id"], "job_name": d["job"]["name"], "trigger": d["trigger"],
                "status": d["status"], "created": d["created"], "started": d["started"], "ended": d["ended"],
                "attempt": d["attempt"], "retry_at": d["retry_at"], "error": d["error"],
                "transferred": d["transferred"],
                "units_done": sum(u["status"] == "done" for u in units), "units_total": len(units),
            }

    def detail(self) -> dict:
        with self.lock:
            d = json.loads(json.dumps(self.d))  # deep copy
        units = d["units"] or []
        known = [u["size"] for u in units if u.get("size") is not None]
        total = sum(known) if units and len(known) == len(units) else (sum(known) or None)
        transferred = 0
        for u in units:
            got = u.get("transferred", 0)
            if u["status"] == "done" and u.get("size") is not None:
                got = max(got, u["size"])
            transferred += min(got, u["size"]) if u.get("size") is not None else got
        active = self.active_seconds()
        avg = transferred / active if active > 30 and transferred else None
        eta = None
        if avg and total:
            rate = min(avg, DRIVE_DAILY_LIMIT / 86400) if d["job"].get("drive_stop_on_upload_limit") else avg
            eta = max(0, total - transferred) / rate
        current = next((u for u in units if u["status"] == "running"), None)
        return {
            **self.summary(),
            "job": d["job"],
            "units": units,
            "current": current,
            "live": self.live if d["status"] == "running" else None,
            "total_bytes": total,
            "sizes_complete": bool(units) and len(known) == len(units),
            "transferred_bytes": transferred,
            "verified_bytes": d["verified"],
            "active_seconds": active,
            "avg_speed": avg,
            "max_speed": d["max_speed"],
            "eta_seconds": eta,
            "today_bytes": d["days"].get(time.strftime("%Y-%m-%d"), 0),
            "log": self.log_tail(),
        }


class RunManager:
    def __init__(self, data: DataDir, settings: Settings, rclone: Rclone, jobs: JobStore):
        self.data = data
        self.settings = settings
        self.rclone = rclone
        self.jobs = jobs
        self.runs: dict[str, Run] = {}       # queued / running / waiting_retry
        self.lock = threading.RLock()
        self.keep_awake = KeepAwake()
        self._stop = threading.Event()
        self._restore()

    # ------------------------------------------------------------ bookkeeping

    def _restore(self) -> None:
        """Pick up runs that were active when cloudhop last stopped."""
        for folder in sorted(self.data.runs.iterdir()):
            if not folder.is_dir():
                continue
            run = Run.load(folder)
            if not run or run.d["status"] not in LIVE_STATUSES:
                continue
            if run.d["status"] == "running":
                run.d["status"] = "queued"
                run.d["trigger"] = "resume"
                run.log("cloudhop was restarted; resuming this run")
                for u in run.d["units"] or []:
                    if u["status"] == "running":
                        u["status"] = "pending"
                run.save(force=True)
            self.runs[run.id] = run

    def history(self, job_id: str | None = None, limit: int = 50) -> list[dict]:
        out = []
        for folder in sorted(self.data.runs.iterdir(), reverse=True):
            if len(out) >= limit:
                break
            run = self.runs.get(folder.name) or Run.load(folder)
            if run and (not job_id or run.d["job_id"] == job_id):
                out.append(run.summary())
        return out

    def get(self, run_id: str) -> Run | None:
        if run_id in self.runs:
            return self.runs[run_id]
        folder = self.data.runs / run_id
        return Run.load(folder) if folder.is_dir() and "/" not in run_id and ".." not in run_id else None

    def active_for_job(self, job_id: str) -> Run | None:
        return next((r for r in self.runs.values() if r.d["job_id"] == job_id), None)

    # ------------------------------------------------------------ control

    def start(self, job_id: str, trigger: str = "manual") -> Run:
        job = self.jobs.get(job_id)
        if not job:
            raise KeyError("No such job")
        with self.lock:
            existing = self.active_for_job(job_id)
            if existing:
                if existing.d["status"] in ("waiting_retry", "paused"):  # "run now" / Resume
                    return self.resume(existing.id)
                raise RcloneError("This job is already running")
            run = Run.create(self.data.runs, job, trigger)
            run.log(f"Run created ({trigger})")
            self.runs[run.id] = run
            return run

    def resume(self, run_id: str) -> Run:
        run = self.runs.get(run_id)
        if not run or run.d["status"] not in ("paused", "waiting_retry"):
            raise KeyError("Only paused or waiting runs can be resumed")
        with run.lock:
            run.d.update(status="queued", trigger="resume", retry_at=None, error=None)
        run.log("Resumed by user")
        run.save(force=True)
        return run

    def stop(self, run_id: str) -> None:
        run = self.runs.get(run_id)
        if not run:
            raise KeyError("Run is not active")
        run.cancel_event.set()
        with run.lock:
            if run.d["status"] in ("queued", "waiting_retry", "paused"):
                run.d.update(status="cancelled", ended=time.time(), retry_at=None)
                run.save(force=True)
                self.runs.pop(run_id, None)
        proc = run.proc
        if proc and proc.poll() is None:
            if sys.platform == "win32":
                proc.terminate()
            else:
                proc.send_signal(signal.SIGINT)  # lets rclone finish cleanly

    def shutdown(self) -> None:
        self._stop.set()
        for run in list(self.runs.values()):
            if run.proc and run.proc.poll() is None:
                run.proc.terminate()
        self.keep_awake.set(False)

    # ------------------------------------------------------------ scheduler loop

    def loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:  # never let the scheduler die
                print("scheduler error:", e, file=sys.stderr)
            self._stop.wait(5)

    def _tick(self) -> None:
        now = time.time()
        for job in self.jobs.all():
            due = job.get("next_run")
            if job["enabled"] and due and now >= due:
                self.jobs.set_next_run(job["id"], next_run(job["schedule"], now))
                try:
                    self.start(job["id"], "schedule")
                except RcloneError:
                    pass  # still running from last time; skip this slot
        with self.lock:
            for run in self.runs.values():
                if run.d["status"] == "waiting_retry" and run.d["retry_at"] and now >= run.d["retry_at"]:
                    run.d.update(status="queued", trigger="retry", retry_at=None)
                    run.save(force=True)
            running = sum(r.d["status"] == "running" for r in self.runs.values())
            for run in sorted(self.runs.values(), key=lambda r: r.d["created"]):
                if running >= self.settings.get("max_concurrent_runs"):
                    break
                if run.d["status"] == "queued":
                    run.d["status"] = "running"
                    run.save(force=True)
                    threading.Thread(target=self._execute, args=(run,), daemon=True).start()
                    running += 1
            self.keep_awake.set(self.settings.get("keep_awake") and running > 0)

    # ------------------------------------------------------------ execution

    def _execute(self, run: Run) -> None:
        job = run.d["job"]
        with run.lock:
            run.cancel_event.clear()
            run.d["attempt"] += 1
            run.d["started"] = run.d["started"] or time.time()
            run.d["error"] = None
            run._attempt_started = time.time()
        run.log(f"Attempt {run.d['attempt']}: {job['mode']} {job['source']} -> {job['destination']}")
        try:
            if run.d["units"] is None:
                run.d["units"] = self._discover_units(run)
                run.save(force=True)
            if job["scan_sizes"]:
                threading.Thread(target=self._scan_sizes, args=(run,), daemon=True).start()
            failed = 0
            for unit in run.d["units"]:
                if unit["status"] == "done":
                    continue
                if run.cancel_event.is_set():
                    raise Cancelled()
                try:
                    self._process_unit(run, unit)
                except NeedsAttention:
                    run.set_unit(unit, status="pending", phase="")
                    raise
                except (RcloneError, OSError) as e:
                    failed += 1
                    run.set_unit(unit, status="failed", phase="", error=str(e))
                    run.log(f"FAILED {unit['name']}: {e}")
            if failed:
                raise RcloneError(f"{failed} folder(s) failed; see the log")
            self._finish(run, "succeeded")
        except Cancelled:
            self._finish(run, "cancelled")
        except NeedsAttention as e:
            self._finish(run, "paused", str(e))
        except StopRun as e:
            self._finish(run, "failed", str(e))
        except Exception as e:
            self._finish(run, "failed", str(e))

    def _finish(self, run: Run, status: str, error: str | None = None) -> None:
        job = run.d["job"]
        with run.lock:
            run.d["active_seconds"] = run.active_seconds()
            run._attempt_started = None
            run.live = None
            for u in run.d["units"] or []:
                if u["status"] == "running":
                    u["status"] = "pending"
            retry = status == "failed" and run.d["attempt"] <= job["retry_max"]
            if retry:
                run.d.update(status="waiting_retry", error=error,
                             retry_at=time.time() + job["retry_minutes"] * 60)
            elif status == "paused":
                run.d.update(status="paused", error=error)
            else:
                run.d.update(status=status, error=error, ended=time.time())
        run.log(f"Run {run.d['status']}" + (f": {error}" if error else "")
                + (f" (retry in {job['retry_minutes']} min)" if run.d["status"] == "waiting_retry" else ""))
        run.save(force=True)
        if run.d["status"] not in ("waiting_retry", "paused"):
            with self.lock:
                self.runs.pop(run.id, None)

    def _discover_units(self, run: Run) -> list[dict]:
        job = run.d["job"]

        def unit(name, path, scope):
            return {"name": name, "path": path, "scope": scope, "status": "pending", "phase": "",
                    "detail": "", "size": None, "count": None, "transferred": 0, "files": 0,
                    "error": None, "updated": None}

        if not job["batch_by_folder"]:
            return [unit(WHOLE_UNIT, "", [])]
        run.log("Listing top-level folders…")
        entries = self.rclone.run_json("lsjson", job["source"], "--dirs-only", "--max-depth", "1",
                                       "--no-modtime", "--no-mimetype", timeout=600)
        skip = set()
        if job["archive_to"]:  # never treat the archive folder itself as work
            src_remote, src_sub = split_remote(job["source"])
            arc_remote, arc_sub = split_remote(job["archive_to"])
            prefix = src_sub + "/" if src_sub else ""
            if arc_remote == src_remote and arc_sub.startswith(prefix):
                skip.add(arc_sub[len(prefix):].split("/")[0])
        names = sorted((e["Name"] for e in entries if e["Name"] not in skip), key=str.lower)
        loose = self.rclone.run_json("lsjson", job["source"], "--files-only", "--max-depth", "1",
                                     "--no-modtime", "--no-mimetype", timeout=600)
        units = [unit(ROOT_UNIT, "", ["--max-depth", "1"])] if loose else []
        return units + [unit(n, n, []) for n in names]

    def _scan_sizes(self, run: Run) -> None:
        job = run.d["job"]
        for unit in list(run.d["units"]):
            if unit.get("size") is not None or run.d["status"] != "running":
                continue
            try:
                args = ["size", "--json", join(job["source"], unit["path"]), *unit["scope"],
                        *filter_flags(job, split=False)]
                if job["tpslimit"]:
                    args += ["--tpslimit", str(job["tpslimit"])]
                res = self.rclone.run_json(*args, timeout=6 * 3600)
                with run.lock:
                    if unit.get("size") is None:
                        unit["size"], unit["count"] = res.get("bytes", 0), res.get("count", 0)
                run.save()
            except Exception as e:
                run.log(f"Size scan of {unit['name']} failed: {e}")

    def _process_unit(self, run: Run, unit: dict) -> None:
        job = run.d["job"]
        src = join(job["source"], unit["path"])
        is_top_root = unit["name"] == ROOT_UNIT
        pairs = [{"label": "main", "dst": join(job["destination"], unit["path"]),
                  "filters": filter_flags(job)}]
        for r in job["routes"]:
            pairs.append({"label": r["extensions"], "dst": join(r["destination"], unit["path"]),
                          "filters": filter_flags(job, r)})
        run.set_unit(unit, status="running", error=None)
        run.log(f"== {unit['name']}")

        # Retry after a failed archive/delete step: the files are already verified,
        # so go straight to step 3 instead of transferring and verifying again.
        verified_file = run.folder / f"unit-{run.d['units'].index(unit)}.verified"
        if unit.get("stage") == "verified" and verified_file.exists():
            run.log("Already verified in an earlier attempt; continuing with the next step")
            matched_all = [ln for ln in verified_file.read_text(encoding="utf-8").splitlines() if ln]
            self._after_verify(run, unit, src, matched_all, is_top_root)
            return self._unit_done(run, unit)

        # 1. transfer
        for p in pairs:
            run.set_unit(unit, phase="transfer", detail=p["label"])
            cmd = "copy" if p["label"] != "main" else job["mode"]
            args = [cmd, src, p["dst"], *unit["scope"], *p["filters"], *transfer_flags(job)]
            if cmd == "move" and not is_top_root:
                args.append("--delete-empty-src-dirs")
            if job["only_new"]:
                args.append("--ignore-existing")
            code, final = self._rclone(run, unit, args, account=True)
            p["seen"] = (final or {}).get("totalTransfers", 0) + (final or {}).get("totalChecks", 0)
            self._check_quota(run, code, p["dst"])
            if code in FATAL_EXIT:
                raise StopRun(f"rclone stopped (exit {code}): {self._last_error(run) or 'limit reached'}")
            if code != 0:
                raise RcloneError(f"transfer to {p['dst']} failed (exit {code}): {self._last_error(run)}")

        # 2. verify
        matched_all = []
        if job["verify"] != "none":
            for i, p in enumerate(pairs):
                if not p.get("seen"):
                    continue  # nothing of this kind in this folder
                run.set_unit(unit, phase="verify", detail=p["label"])
                matched = run.folder / f"verified-{uuid.uuid4().hex[:8]}.txt"
                code = self._verify(run, unit, src, p, matched)
                self._check_quota(run, code, p["dst"])
                if code in FATAL_EXIT:
                    raise StopRun(f"rclone stopped during verification (exit {code})")
                if code != 0:
                    raise RcloneError(f"verification against {p['dst']} failed: {self._last_error(run)}")
                if matched.exists():
                    matched_all += [ln for ln in matched.read_text(encoding="utf-8").splitlines() if ln]
                    matched.unlink()
            verified_file.write_text("\n".join(matched_all) + ("\n" if matched_all else ""), encoding="utf-8")
            run.set_unit(unit, files=len(matched_all), stage="verified")

        # 3. archive or delete verified source files
        self._after_verify(run, unit, src, matched_all, is_top_root)
        self._unit_done(run, unit)

    def _after_verify(self, run: Run, unit: dict, src: str, matched: list[str], is_top_root: bool) -> None:
        job = run.d["job"]
        action = job.get("after_verify", "keep")
        if action == "keep" or not matched:
            return
        run.set_unit(unit, phase=action, detail=f"{len(matched)} files")
        listfile = run.folder / f"{action}-{uuid.uuid4().hex[:8]}.txt"
        listfile.write_text("\n".join(matched) + "\n", encoding="utf-8")
        if action == "archive":
            args = ["move", src, join(job["archive_to"], unit["path"]), "--files-from-raw", str(listfile),
                    "--checkers", str(job["checkers"]), "--transfers", str(job["transfers"])]
        else:
            args = ["delete", src, "--files-from-raw", str(listfile), "--checkers", str(job["checkers"])]
        if job["tpslimit"]:
            args += ["--tpslimit", str(job["tpslimit"])]
        code, _ = self._rclone(run, unit, args)
        self._check_quota(run, code, src)
        if code in FATAL_EXIT:
            raise StopRun(f"rclone stopped while trying to {action} files: {self._last_error(run)}")
        if code != 0:
            raise RcloneError(f"{action} step failed (exit {code}): {self._last_error(run)}")
        listfile.unlink(missing_ok=True)
        if not is_top_root:  # remove folders left empty (never touches anything with files in it)
            self._rclone(run, unit, ["rmdirs", src] + ([] if unit["path"] else ["--leave-root"]))

    def _unit_done(self, run: Run, unit: dict) -> None:
        job = run.d["job"]
        with run.lock:
            size = unit.get("size")
            if job["verify"] != "none":
                run.d["verified"] += size if size is not None else unit.get("transferred", 0)
        n = unit.get("files", 0)
        detail = f"{n} file{'' if n == 1 else 's'} verified" if job["verify"] != "none" else ""
        run.set_unit(unit, status="done", phase="", detail=detail)
        run.save(force=True)

    def _verify(self, run: Run, unit: dict, src: str, p: dict, matched: Path) -> int:
        job = run.d["job"]
        base = [*unit["scope"], *p["filters"], "--one-way", "--match", str(matched)]
        if job["tpslimit"]:
            base += ["--tpslimit", str(job["tpslimit"])]
        if job["verify"] == "size":
            return self._rclone(run, unit, ["check", src, p["dst"], "--size-only", *base])[0]
        src_h, dst_h = self.rclone.hashes(src), self.rclone.hashes(p["dst"])
        common = [h for h in src_h if h in dst_h]
        if common:
            # Both sides know a hash: compare without downloading anything
            return self._rclone(run, unit, ["check", src, p["dst"], *base])[0]
        if src_h:
            # Source hashes are free; download the destination side and hash it in memory
            h = src_h[0]
            sums = run.folder / f"sums-{uuid.uuid4().hex[:8]}.{h}"
            code, _ = self._rclone(run, unit, ["hashsum", h, src, *unit["scope"], *p["filters"],
                                               "--output-file", str(sums)])
            if code != 0:
                return code
            if not sums.exists() or sums.stat().st_size == 0:
                return 0
            return self._rclone(run, unit, ["checksum", h, str(sums), p["dst"], *base, "--download",
                                            "--drive-acknowledge-abuse"])[0]
        return self._rclone(run, unit, ["check", src, p["dst"], *base, "--download",
                                        "--drive-acknowledge-abuse"])[0]

    def _rclone(self, run: Run, unit: dict, args: list[str], account: bool = False) -> tuple[int, dict | None]:
        """Run one rclone command, streaming its JSON log into stats and the run log."""
        if run.cancel_event.is_set():
            raise Cancelled()
        cmd = self.rclone.base_cmd() + args + ["--use-json-log", "--stats", "2s",
                                               "--stats-log-level", "NOTICE", "-v"]
        run.log("$ rclone " + " ".join(_quote(a) for a in args))
        run.last_error = None
        run.quota_error = None
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                                encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
        run.proc = proc
        prev_bytes = 0
        final = None
        for line in proc.stderr:
            try:
                entry = json.loads(line)
            except ValueError:
                run.log(line.rstrip())
                continue
            stats = entry.get("stats")
            if stats:
                final = stats
                with run.lock:
                    run.live = {**stats, "unit": unit["name"], "phase": unit.get("phase"),
                                "detail": unit.get("detail")}
                    speed = stats.get("speed") or 0
                    if speed > run.d["max_speed"]:
                        run.d["max_speed"] = speed
                if account:
                    run.add_transferred(unit, stats.get("bytes", 0) - prev_bytes)
                    prev_bytes = max(prev_bytes, stats.get("bytes", 0))
                run.save()
                continue
            level = entry.get("level", "info").upper()
            obj = entry.get("object")
            msg = entry.get("msg", "").strip()
            if level in ("ERROR", "CRITICAL"):
                run.last_error = f"{obj}: {msg}" if obj else msg
                if QUOTA_RE.search(msg):
                    run.quota_error = msg
            run.log(f"{level:<6} {obj + ': ' if obj else ''}{msg}")
        proc.wait()
        run.proc = None
        if run.cancel_event.is_set():
            raise Cancelled()
        return proc.returncode, final

    @staticmethod
    def _check_quota(run: Run, code: int, where: str) -> None:
        if code != 0 and getattr(run, "quota_error", None):
            remote = where.split(":", 1)[0] if ":" in where else where
            raise NeedsAttention(
                f"The storage on '{remote}' is full ({run.quota_error}). Free up some space there "
                f"(or empty its recycle bin), then press Resume. Work already verified is kept.")

    @staticmethod
    def _last_error(run: Run) -> str:
        return getattr(run, "last_error", None) or ""


def _quote(arg: str) -> str:
    return f"'{arg}'" if (" " in arg or not arg) else arg
