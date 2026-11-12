from datetime import datetime

import pytest

from cloudhop.jobs import JobError, join, next_run, split_remote, validate


def job(**kw):
    return {"name": "t", "source": "a:src", "destination": "b:dst", **kw}


def test_defaults_and_paths():
    j = validate(job())
    assert j["mode"] == "copy" and j["verify"] == "none" and j["after_verify"] == "keep"
    assert split_remote("onedrive:") == ("onedrive:", "")
    assert split_remote("g:a/b/") == ("g:", "a/b")
    assert join("onedrive:", "Photos") == "onedrive:Photos"
    assert join("g:backup", "Photos") == "g:backup/Photos"
    assert join("g:x", "") == "g:x"
    assert split_remote("C:\\Users\\me\\src") == ("C:", "Users/me/src")


@pytest.mark.parametrize("bad", [
    {"source": ""},
    {"destination": "b:dst", "source": "b:dst"},
    {"mode": "move", "verify": "content"},
    {"after_verify": "archive", "verify": "content", "archive_to": "other:arch"},
    {"after_verify": "delete"},  # needs verification
    {"after_verify": "archive", "verify": "size"},  # needs a folder
    {"routes": [{"extensions": "", "destination": "c:raw"}]},
    {"mode": "sync", "routes": [{"extensions": "cr2", "destination": "c:raw"}]},
    {"max_age": "yesterday"},
    {"extra_flags": "--config /etc/passwd"},
    {"schedule": {"type": "weekly", "days": []}},
    {"schedule": {"type": "daily", "time": "25:00"}},
])
def test_rejects_bad_jobs(bad):
    with pytest.raises(JobError):
        validate(job(**bad))


def test_archive_needs_same_remote_and_routes_normalised():
    j = validate(job(verify="content", after_verify="archive", archive_to="a:_migrated",
                     routes=[{"extensions": ".CR2, nef", "destination": "c:RAW"}]))
    assert j["archive_to"] == "a:_migrated"
    assert j["routes"] == [{"extensions": "cr2,nef", "destination": "c:RAW"}]


def test_schedules():
    base = datetime(2026, 11, 12, 10, 0).timestamp()  # a Thursday
    assert next_run({"type": "manual"}, base) is None
    assert next_run({"type": "interval", "every_minutes": 90}, base) == base + 5400
    daily = next_run({"type": "daily", "time": "02:00", "days": []}, base)
    assert datetime.fromtimestamp(daily) == datetime(2026, 11, 13, 2, 0)
    later_today = next_run({"type": "daily", "time": "23:30", "days": []}, base)
    assert datetime.fromtimestamp(later_today) == datetime(2026, 11, 12, 23, 30)
    weekly = next_run({"type": "weekly", "time": "08:00", "days": [0]}, base)  # Monday
    assert datetime.fromtimestamp(weekly) == datetime(2026, 11, 16, 8, 0)
