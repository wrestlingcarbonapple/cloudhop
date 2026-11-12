"""End-to-end runs against local folders with the real rclone binary."""

import shutil
import time
from pathlib import Path

import pytest

from cloudhop.jobs import JobStore
from cloudhop.rclone import Rclone
from cloudhop.runner import NeedsAttention, Run, RunManager
from cloudhop.store import DataDir, Settings

pytestmark = pytest.mark.skipif(not shutil.which("rclone"), reason="rclone not installed")


@pytest.fixture
def env(tmp_path):
    data = DataDir(tmp_path / "data")
    settings = Settings(data)
    rclone = Rclone(data, settings)
    jobs = JobStore(data)
    runs = RunManager(data, settings, rclone, jobs)
    src = tmp_path / "src"
    for rel, content in {
        "loose.txt": "root file",
        "Photos/a.jpg": "jpg-a",
        "Photos/b.CR2": "raw-b" * 100,
        "Photos/2020/c.jpg": "jpg-c",
        "Docs/report.pdf": "pdf" * 1000,
    }.items():
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_text(content)
    return tmp_path, jobs, runs


def wait(runs, run, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        runs._tick()
        if run.d["status"] not in ("queued", "running"):
            return run
        time.sleep(0.3)
    raise AssertionError(f"run did not finish: {run.d['status']}")


def files(root: Path):
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def test_batch_routes_verify_archive(env):
    tmp, jobs, runs = env
    job = jobs.save({
        "name": "e2e", "source": str(tmp / "src"), "destination": str(tmp / "dst"),
        "batch_by_folder": True, "verify": "content", "after_verify": "archive",
        "archive_to": str(tmp / "src" / "_done"),
        "routes": [{"extensions": "cr2", "destination": str(tmp / "raw")}],
    })
    run = wait(runs, runs.start(job["id"]))
    assert run.d["status"] == "succeeded", run.log_tail()
    assert files(tmp / "dst") == ["Docs/report.pdf", "Photos/2020/c.jpg", "Photos/a.jpg", "loose.txt"]
    assert files(tmp / "raw") == ["Photos/b.CR2"]
    # Everything verified was archived; empty folders removed; archive folder not treated as work
    assert files(tmp / "src") == ["_done/Docs/report.pdf", "_done/Photos/2020/c.jpg", "_done/Photos/a.jpg",
                                  "_done/Photos/b.CR2", "_done/loose.txt"]
    assert [u["name"] for u in run.d["units"]] == ["(files in the top folder)", "Docs", "Photos"]
    detail = run.detail()
    assert detail["transferred_bytes"] > 0 and detail["verified_bytes"] > 0
    # A second run finds nothing left to do
    run2 = wait(runs, runs.start(job["id"]))
    assert run2.d["status"] == "succeeded", run2.log_tail()
    assert "_done" not in [u["name"] for u in run2.d["units"]]


def test_download_verification_and_delete(env, monkeypatch):
    tmp, jobs, runs = env
    # Pretend the destination has no hash in common with the source: forces hashsum + checksum --download
    monkeypatch.setattr(runs.rclone, "hashes", lambda fs: ["md5"] if "src" in fs else [])
    job = jobs.save({"name": "dl", "source": str(tmp / "src"), "destination": str(tmp / "dst"),
                     "verify": "content", "after_verify": "delete"})
    run = wait(runs, runs.start(job["id"]))
    assert run.d["status"] == "succeeded", run.log_tail()
    assert "checksum" in "\n".join(run.log_tail())
    assert files(tmp / "src") == []
    assert len(files(tmp / "dst")) == 5


def test_storage_full_pauses_instead_of_failing(env):
    tmp, _, runs = env
    run = Run(tmp, {"id": "x"})
    run.quota_error = "quotaLimitReached: Quota limit reached"
    with pytest.raises(NeedsAttention, match="Free up some space"):
        runs._check_quota(run, 7, "onedrive:Photos")
    run.quota_error = None
    runs._check_quota(run, 1, "onedrive:Photos")  # other errors are not a pause


def test_paused_run_resumes_and_skips_verified_work(env, monkeypatch):
    tmp, jobs, runs = env
    job = jobs.save({"name": "p", "source": str(tmp / "src"), "destination": str(tmp / "dst"),
                     "verify": "content", "after_verify": "archive", "archive_to": str(tmp / "src" / "_done")})
    original = runs._after_verify
    calls = {"n": 0}

    def flaky(*args, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise NeedsAttention("The storage on 'src' is full")
        return original(*args, **kw)

    monkeypatch.setattr(runs, "_after_verify", flaky)
    run = wait(runs, runs.start(job["id"]))
    assert run.d["status"] == "paused" and "full" in run.d["error"]
    assert run.d["units"][0]["stage"] == "verified"
    runs.resume(run.id)
    run = wait(runs, run)
    assert run.d["status"] == "succeeded", run.log_tail()
    assert "Already verified in an earlier attempt" in "\n".join(run.log_tail())
    assert len(files(tmp / "src" / "_done")) == 5


def test_verify_while_copying_when_no_common_hash(env, monkeypatch):
    tmp, jobs, runs = env
    # Like OneDrive (quickxor only) -> Google Drive (md5 only): copy through a hasher overlay
    monkeypatch.setattr(runs.rclone, "hashes", lambda fs: ["quickxor"] if "src" in fs else ["md5"])
    job = jobs.save({"name": "h", "source": str(tmp / "src"), "destination": str(tmp / "dst"),
                     "verify": "content", "batch_by_folder": True})
    run = wait(runs, runs.start(job["id"]))
    log = "\n".join(Path(run.folder / "log.txt").read_text().splitlines())
    assert run.d["status"] == "succeeded", run.log_tail()
    assert "recorded while copying" in log
    assert "rclone copy cloudhophash" in log            # copies went through the overlay
    assert "checking by download" not in log and "--download" not in log
    assert sum(u["files"] for u in run.d["units"]) == 5
    # The user's rclone config was not touched
    assert not (tmp / "data" / "rclone.conf").exists() or "hasher" not in (tmp / "data" / "rclone.conf").read_text()
