"""Google Photos destination, exercised against a local folder standing in for Photos."""

import shutil
import time

import pytest

from cloudhop import photos
from cloudhop.jobs import JobError, JobStore
from cloudhop.rclone import Rclone
from cloudhop.runner import RunManager
from cloudhop.store import DataDir, Settings

pytestmark = pytest.mark.skipif(not shutil.which("rclone"), reason="rclone not installed")


def test_destination_and_rules():
    assert photos.destination_fs("gp:") == "gp:upload"
    assert photos.destination_fs("gp:album/Holiday 2019") == "gp:album/Holiday 2019"
    with pytest.raises(JobError):
        photos.destination_fs("gp:album/a/b")
    job = {"destination": "gp:", "mode": "copy", "routes": [], "verify": "content",
           "after_verify": "archive", "archive_to": "x:"}
    assert photos.validate_photos_job(job)["verify"] == "none"
    with pytest.raises(JobError):
        photos.validate_photos_job({**job, "mode": "sync"})


def test_unique_names():
    files = [{"Name": "DSC_1.JPG", "Path": "2015/2015-04-03/DSC_1.JPG"},
             {"Name": "DSC_1.JPG", "Path": "2016/2016-01-01/DSC_1.JPG"},
             {"Name": "solo.jpg", "Path": "x/solo.jpg"},
             {"Name": "taken.jpg", "Path": "y/taken.jpg"}]
    photos.unique_names(files, {"taken.jpg"})
    assert [f["upload_name"] for f in files] == [
        "2015-04-03 DSC_1.JPG", "2016-01-01 DSC_1.JPG", "solo.jpg", "y taken.jpg"]


def test_upload_dedupes_filters_and_resumes(tmp_path, monkeypatch):
    data = DataDir(tmp_path / "data")
    settings = Settings(data)
    rclone = Rclone(data, settings)
    jobs = JobStore(data)
    runs = RunManager(data, settings, rclone, jobs)
    src, dst = tmp_path / "src", tmp_path / "photos"
    for rel, content in {"2015/a/IMG_1.jpg": "one", "2016/b/IMG_1.jpg": "two", "copy/IMG_9.jpg": "one",
                         "2016/b/clip.mp4": "vid", "2016/b/IMG_1.xmp": "sidecar", "notes.txt": "x"}.items():
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_text(content)
    monkeypatch.setattr(rclone, "remote_type", lambda path: photos.PHOTOS_TYPE if "photos" in path else "local")
    monkeypatch.setattr(photos, "destination_fs", lambda d: str(dst))
    job = jobs.save({"name": "p", "source": str(src), "destination": str(dst), "photos_videos": True})

    def wait(run):
        for _ in range(200):
            runs._tick()
            if run.d["status"] not in ("queued", "running"):
                return run
            time.sleep(0.2)
        raise AssertionError(run.d["status"])

    run = wait(runs.start(job["id"]))
    assert run.d["status"] == "succeeded", run.log_tail()
    uploaded = list(dst.iterdir())
    # Same content once (IMG_9 == one IMG_1), .xmp/.txt are not media, names never clash
    assert sorted(p.read_text() for p in uploaded) == ["one", "two", "vid"]
    assert len({p.name.lower() for p in uploaded}) == 3
    assert run.d["units"][0]["files"] == 3
    # A second run uploads nothing: everything is in the ledger
    run2 = wait(runs.start(job["id"]))
    assert run2.d["status"] == "succeeded" and "already in Google Photos" in "\n".join(run2.log_tail())
    assert len(list(dst.iterdir())) == 3
