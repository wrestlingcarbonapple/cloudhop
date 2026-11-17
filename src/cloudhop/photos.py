"""Google Photos as a destination.

Google Photos is not a folder tree: everything lands in one library (optionally in an
album). So a Photos unit lists the source recursively, keeps only formats Google
accepts within its size limits, skips content already uploaded (by MD5, kept in a
ledger per Photos connection), gives clashing names a folder prefix, and uploads each
file with `operations/copyfile` through a short-lived `rclone rcd`.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from cloudhop.jobs import JobError, split_remote

PHOTO = {"jpg", "jpeg", "png", "webp", "gif", "heic", "heif", "avif", "bmp", "ico", "tif", "tiff"}
VIDEO = {"3gp", "3g2", "asf", "avi", "divx", "m2t", "m2ts", "m4v", "mkv", "mmv", "mod", "mov", "mp4",
         "mpg", "mpeg", "mts", "tod", "wmv"}
LIMIT = {"photo": 200 * 2**20, "video": 20 * 2**30}   # Photos Library API upload limits
QUOTA_HINTS = ("quota", "rate limit", "resource_exhausted", "429", "too many requests")
PHOTOS_TYPE = "google photos"


class PhotosQuota(Exception):
    """Google's daily upload quota was reached: wait and continue later."""


def media_kind(name: str, videos: bool) -> str | None:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext in PHOTO:
        return "photo"
    if videos and ext in VIDEO:
        return "video"
    return None


def destination_fs(destination: str) -> str:
    """'gphotos:' -> library without album; 'gphotos:album/Name' -> that album."""
    remote, sub = split_remote(destination)
    if not sub or sub == "upload":
        return remote + "upload"
    parts = sub.split("/")
    if parts[0] != "album" or len(parts) != 2 or not parts[1]:
        raise JobError("For Google Photos use 'connection:' (library, no album) or "
                       "'connection:album/Album name' (everything in one album)")
    return f"{remote}album/{parts[1]}"


def validate_photos_job(job: dict) -> dict:
    """Apply the rules of a Google Photos destination to an already validated job."""
    destination_fs(job["destination"])
    if job["mode"] != "copy":
        raise JobError("Google Photos only supports copying (it can't mirror or delete).")
    if job["routes"]:
        raise JobError("File-type routes don't apply to Google Photos; use a separate job.")
    # Photos can't hand back originals for checking; success means Google created the item
    job["verify"], job["after_verify"], job["archive_to"] = "none", "keep", ""
    return job


def unique_names(files: list[dict], taken: set[str]) -> None:
    """Keep the original name unless it clashes; then prefix folder names until unique."""
    counts = Counter(f["Name"].lower() for f in files)
    used = {n.lower() for n in taken}
    for f in files:
        parts = f["Path"].split("/")[:-1]
        name = f["Name"]
        if counts[name.lower()] > 1 or name.lower() in used:
            depth = 1
            while True:
                prefix = " ".join(parts[-depth:]) if depth <= len(parts) else (f.get("md5") or "")[:8]
                name = f"{prefix} {f['Name']}".strip()
                if name.lower() not in used or depth > len(parts):
                    break
                depth += 1
        used.add(name.lower())
        f["upload_name"] = name


class Ledger:
    """What has been uploaded to one Photos connection: md5 -> name. Shared by all jobs."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.md5: dict[str, str] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                parts = line.split("\t")
                if len(parts) >= 2:
                    self.md5[parts[0]] = parts[1]

    def add(self, md5: str, name: str, source: str) -> None:
        with self.lock:
            self.md5[md5] = name
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(f"{md5}\t{name}\t{source}\t{int(time.time())}\n")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _rc(port: int, method: str, params: dict, timeout: float | None = None) -> dict:
    req = urllib.request.Request(f"http://127.0.0.1:{port}/{method}", data=json.dumps(params).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            msg = json.loads(body).get("error", body)
        except ValueError:
            msg = body
        raise RuntimeError(msg) from None


def upload_unit(manager, run, unit: dict, src: str, ledger: Ledger, dst_fs: str, workers: int = 8) -> None:
    """Upload one unit's media into Google Photos. Raises PhotosQuota when Google says stop."""
    job = run.d["job"]
    run.set_unit(unit, phase="list", detail="")
    listing = manager.rclone.run_json("lsjson", "-R", "--files-only", "--hash", "--hash-type", "md5",
                                      "--no-mimetype", src, *unit["scope"], timeout=6 * 3600)
    media, skipped, seen = [], Counter(), set()
    for f in listing:
        k = media_kind(f["Name"], job.get("photos_videos", True))
        f["md5"] = (f.get("Hashes") or {}).get("md5", "")
        if not k:
            skipped["not a photo/video Google accepts"] += 1
        elif f["Size"] > LIMIT[k]:
            skipped["over Google's size limit"] += 1
        elif f["md5"] and f["md5"] in ledger.md5:
            skipped["already in Google Photos"] += 1
        elif f["md5"] and f["md5"] in seen:
            skipped["duplicate"] += 1
        else:
            seen.add(f["md5"])
            media.append(f)
    unique_names(media, set(ledger.md5.values()))
    total = sum(f["Size"] for f in media)
    with run.lock:
        if unit.get("size") is None:
            unit["size"], unit["count"] = total, len(media)
    run.log(f"{len(media)} files to upload ({total / 1e9:.1f} GB); skipped: {dict(skipped) or 'none'}")
    if not media:
        run.set_unit(unit, files=0, detail=", ".join(f"{n} {k}" for k, n in skipped.items()))
        return

    port = _free_port()
    env = {**os.environ, **getattr(run, "env", {})} if getattr(run, "env", None) else None
    daemon = subprocess.Popen(manager.rclone.base_cmd() + ["rcd", "--rc-addr", f"127.0.0.1:{port}",
                                                           "--rc-no-auth", "-v", "--log-file",
                                                           str(run.folder / "photos-rclone.log")],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    run.proc = daemon
    for _ in range(100):
        try:
            _rc(port, "rc/noop", {}, timeout=2)
            break
        except Exception:
            time.sleep(0.1)

    stop = threading.Event()
    state = {"uploaded": 0, "failed": 0, "quota": None, "prev": 0}
    run.set_unit(unit, phase="transfer", detail="Google Photos")

    def one(f):
        if stop.is_set() or run.cancel_event.is_set():
            return
        try:
            _rc(port, "operations/copyfile", {"srcFs": src, "srcRemote": f["Path"],
                                              "dstFs": dst_fs, "dstRemote": f["upload_name"]})
            if f["md5"]:
                ledger.add(f["md5"], f["upload_name"], f"{src}/{f['Path']}")
            with run.lock:
                state["uploaded"] += 1
        except Exception as e:
            msg = str(e).replace("\n", " ")[:400]
            if any(h in msg.lower() for h in QUOTA_HINTS):
                state["quota"] = msg
                stop.set()
                return
            with run.lock:
                state["failed"] += 1
            run.log(f"ERROR  {f['Path']}: {msg}")

    def stats_loop():
        while not stop.wait(2):
            try:
                s = _rc(port, "core/stats", {}, timeout=5)
            except Exception:
                continue
            with run.lock:
                run.live = {**s, "unit": unit["name"], "phase": "transfer", "detail": "Google Photos",
                            "totalBytes": total, "totalTransfers": len(media),
                            "transfers": state["uploaded"]}
                run.d["max_speed"] = max(run.d["max_speed"], s.get("speed") or 0)
            run.add_transferred(unit, s.get("bytes", 0) - state["prev"])
            state["prev"] = max(state["prev"], s.get("bytes", 0))
            run.save()

    threading.Thread(target=stats_loop, daemon=True).start()
    try:
        with ThreadPoolExecutor(workers) as pool:
            list(pool.map(one, media))
    finally:
        stop.set()
        daemon.terminate()
        run.proc = None
    run.set_unit(unit, files=state["uploaded"])
    run.log(f"Uploaded {state['uploaded']} of {len(media)}; failed {state['failed']}")
    if run.cancel_event.is_set():
        return
    if state["quota"]:
        raise PhotosQuota(state["quota"])
    if state["failed"]:
        raise RuntimeError(f"{state['failed']} file(s) could not be uploaded; they are retried on the next run")
