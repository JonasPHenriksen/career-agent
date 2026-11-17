from __future__ import annotations

import sqlite3
import zipfile
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from career_agent import backup, config, db, settings, tasks
from career_agent.web.app import app


@pytest.fixture()
def nextcloud(fake, tmp_path, monkeypatch):
    """A home folder with a Nextcloud folder in it, so nothing touches the real ~/Nextcloud."""
    home = tmp_path / "home"
    (home / "Nextcloud").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    (config.FILES_DIR / "cv-photo.jpg").write_bytes(b"\xff\xd8 not really a jpeg")
    return home / "Nextcloud"


def _zips(folder):
    return sorted(p.name for p in folder.glob("*.zip"))


def test_default_is_weekly_and_off_until_a_folder_is_chosen(fake):
    cfg = settings.get("backup")
    assert cfg["every_days"] == 7 and cfg["enabled"] and cfg["folder"] == ""
    assert not backup.due()


def test_overwrite_keeps_one_zip_with_the_database_and_files(nextcloud, tmp_path):
    folder = nextcloud / "Backups" / "Career Agent"
    settings.update("backup", {"folder": str(folder), "mode": "overwrite"})
    backup.run()
    backup.run()
    assert _zips(folder) == ["career-agent-backup.zip"]
    assert not list(folder.glob(".*.part")), "no half-written file is left behind"
    with zipfile.ZipFile(folder / "career-agent-backup.zip") as z:
        names = set(z.namelist())
        assert {"README.txt", "career.db", "files/cv-photo.jpg"} <= names
        z.extract("career.db", tmp_path / "restored")
    copy = sqlite3.connect(tmp_path / "restored" / "career.db")
    stored = copy.execute("SELECT value FROM settings WHERE key = 'profile'").fetchone()[0]
    assert "Sara Lund" in stored, "the backup is a working copy of the database"
    assert settings.get("state")["last_backup_file"] == str(folder / "career-agent-backup.zip")


def test_accumulate_adds_dated_zips_and_keeps_the_newest(nextcloud):
    folder = nextcloud / "Backups"
    folder.mkdir()
    for day in ("2026-10-01", "2026-10-08", "2026-10-15"):
        (folder / f"career-agent-backup-{day}_0900.zip").write_bytes(b"old")
    (folder / "my-notes.zip").write_bytes(b"not ours")
    settings.update("backup", {"folder": str(folder), "mode": "accumulate", "keep": 2})
    result = backup.run()
    left = _zips(folder)
    assert "my-notes.zip" in left, "files the app didn't make are never touched"
    ours = [n for n in left if n.startswith("career-agent-backup-")]
    assert len(ours) == 2 and ours[0] == "career-agent-backup-2026-10-15_0900.zip"
    assert len(result["removed"]) == 2
    settings.update("backup", {"keep": 0})
    for day in ("2026-09-01", "2026-09-08"):
        (folder / f"career-agent-backup-{day}_0900.zip").write_bytes(b"old")
    backup.run()
    assert len([n for n in _zips(folder) if n.startswith("career-agent-backup-")]) >= 4, "keep 0 = keep them all"


def test_folder_checks(nextcloud):
    with pytest.raises(backup.BackupError, match="inside the app's own data folder"):
        backup.check_folder(str(config.DATA_DIR / "backups"))
    with pytest.raises(backup.BackupError, match="doesn't exist"):  # Nextcloud gone: don't quietly make a local folder
        backup.check_folder("~/Nextcloud-old/Backups")
    with pytest.raises(backup.BackupError, match="full path"):
        backup.check_folder("Backups")
    assert backup.check_folder("~/Nextcloud/Backups/Career Agent") == nextcloud / "Backups" / "Career Agent"
    assert backup.suggested_folder() == str(nextcloud / "Backups" / "Career Agent")


def test_due_weekly_and_waits_after_a_failure(nextcloud):
    settings.update("backup", {"folder": str(nextcloud / "B")})
    assert backup.due()
    backup.run()
    assert not backup.due()
    now = db.parse_ts(db.now())
    assert not backup.due(now + timedelta(days=6))
    assert backup.due(now + timedelta(days=7))
    settings.update("backup", {"every_days": 1})
    assert backup.due(now + timedelta(days=1))
    later = now + timedelta(days=2)
    settings.update("state", {"backup_tried_at": (later - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")})
    assert not backup.due(later), "a failed try waits a few hours before the next"
    assert backup.due(later + timedelta(hours=6))
    settings.update("backup", {"enabled": False})
    assert not backup.due(later + timedelta(days=30))


def test_the_backup_task_and_a_missing_nextcloud(nextcloud):
    settings.update("backup", {"folder": str(nextcloud / "Backups")})
    task_id = tasks.enqueue("backup", {}, ref="backup")
    tasks.run_task(db.q1("SELECT * FROM tasks WHERE id = ?", (task_id,)))
    assert db.q1("SELECT status FROM tasks WHERE id = ?", (task_id,))["status"] == "done"
    assert len(_zips(nextcloud / "Backups")) == 1
    nextcloud.rename(nextcloud.with_name("gone"))
    task_id = tasks.enqueue("backup", {}, ref="backup")
    tasks.run_task(db.q1("SELECT * FROM tasks WHERE id = ?", (task_id,)))
    row = db.q1("SELECT status, error FROM tasks WHERE id = ?", (task_id,))
    assert row["status"] == "error" and "doesn't exist" in row["error"]
    assert not (nextcloud / "Backups").exists()


def test_settings_page_save_and_back_up_now(nextcloud):
    c = TestClient(app)
    folder = nextcloud / "Backups" / "Career Agent"
    r = c.get("/settings")
    assert r.status_code == 200 and "Automatic backup" in r.text and str(folder) in r.text  # suggested
    r = c.post("/settings/backup/save", data={"enabled": "1", "folder": str(config.DATA_DIR / "x"), "every_days": "7",
                                              "mode": "accumulate", "keep": "0"}, follow_redirects=False)
    assert r.status_code == 303 and "err=" in r.headers["location"] and settings.get("backup")["folder"] == ""
    r = c.post("/settings/backup/save", data={"enabled": "1", "folder": "~/Nextcloud/Backups/Career Agent", "every_days": "14",
                                              "mode": "overwrite", "keep": "3"}, follow_redirects=False)
    assert r.status_code == 303 and "err=" not in r.headers["location"]
    cfg = settings.get("backup")
    assert cfg == {"enabled": True, "folder": str(folder), "every_days": 14, "mode": "overwrite", "keep": 3}
    assert tasks.active(ref="backup"), "a new folder gets its first backup right away"
    r = c.post("/settings/backup/run", data={"enabled": "1", "folder": str(folder), "every_days": "14", "mode": "overwrite",
                                             "keep": "3"}, follow_redirects=False)
    assert r.status_code == 303 and "Backed+up" in r.headers["location"]
    assert _zips(folder) == ["career-agent-backup.zip"]
    r = c.get("/settings")
    assert "Last backup" in r.text and "1 backup in the folder" in r.text
    r = c.get("/settings/backup")
    assert r.status_code == 200 and r.content[:2] == b"PK"
