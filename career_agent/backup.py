"""Automatic backups of your personal data to a folder you choose, such as one in Nextcloud.

A backup is one zip: a consistent copy of the database (made while the app runs), your files (uploaded documents,
your CV photo) and exported PDFs. Passwords and API keys live in the macOS Keychain and are never in it.

Each backup either goes next to the earlier ones as a new dated file ("accumulate", optionally keeping only the
newest few), or replaces the one backup in the folder ("overwrite"). The app isn't always running, so a backup
that came due while it was off is made soon after the next start.
"""
from __future__ import annotations

import os
import re
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

from . import config, db, settings

PREFIX = "career-agent-backup"
MODES = {"accumulate": "Adds a dated file", "overwrite": "Replaces the last one"}
EVERY = [(1, "Every day"), (3, "Every 3 days"), (7, "Every week"), (14, "Every 2 weeks"), (30, "Every month")]
RETRY_HOURS = 6  # after a failed backup (folder missing, disk full), the next automatic try waits this long
_DATED = re.compile(rf"^{re.escape(PREFIX)}-\d{{4}}-\d{{2}}-\d{{2}}_\d{{4}}\.zip$")

RESTORE_NOTE = """Career Agent backup, made {when}.

What's in it: career.db (your career database, jobs, applications and settings), files/ (documents you uploaded,
your CV photo) and exports/ (PDFs and Word files the app made). Passwords and API keys are in the macOS Keychain
and are not in this backup.

To restore: stop the app (./agent stop). In the app's data folder, move career.db, career.db-wal and career.db-shm
aside, then put this career.db there and copy files/ and exports/ into it. Start the app again.
"""


class BackupError(Exception):
    """The backup can't be made; the message is for you."""


def suggested_folder() -> str:
    """Backups/Career Agent in your Nextcloud folder, when there is one."""
    from . import watch

    found = watch.nextcloud_candidates()
    return str(Path(found[0]) / "Backups" / "Career Agent") if found else ""


def check_folder(raw: str) -> Path:
    """The folder backups go to, checked. It may not exist yet, but the place it would be made in must: a missing
    ~/Nextcloud (the app uninstalled) or an unplugged drive is reported, not quietly made as a local folder."""
    raw = (raw or "").strip()
    if not raw:
        raise BackupError("Choose a folder for the backups first.")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise BackupError("Give the folder's full path, for example ~/Nextcloud/Backups/Career Agent.")
    path = path.resolve()
    data = config.DATA_DIR.resolve()
    if path == data or data in path.parents:
        raise BackupError("That folder is inside the app's own data folder, so a backup there is lost with the data. "
                          "Pick a folder elsewhere, such as in Nextcloud.")
    if path.exists():
        if not path.is_dir():
            raise BackupError(f"{_short(path)} is a file, not a folder.")
        return path
    existing = next((p for p in path.parents if p.exists()), Path("/"))
    if existing in (Path.home().resolve(), Path("/"), Path("/Volumes")):
        missing = path.relative_to(existing).parts[0]
        raise BackupError(f"{_short(existing / missing)} doesn't exist. Is Nextcloud (or the drive) there? "
                          "Create the folder first if it should be.")
    return path


def _short(path: Path) -> str:
    home = str(Path.home())
    s = str(path)
    return "~" + s[len(home):] if s == home or s.startswith(home + os.sep) else s


def write_zip(target: Path) -> dict:
    """Write the backup zip to `target`: the database, your files and exports."""
    counts = {"files": 0}
    with tempfile.TemporaryDirectory() as tmp:
        snap = Path(tmp) / "career.db"
        dst = sqlite3.connect(snap)
        try:
            db.connect().backup(dst)  # consistent even while the app is writing
        finally:
            dst.close()
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("README.txt", RESTORE_NOTE.format(when=datetime.now().strftime("%-d %B %Y, %H:%M")))
            z.write(snap, "career.db")
            for base, name in ((config.FILES_DIR, "files"), (config.EXPORTS_DIR, "exports")):
                if not base.is_dir():
                    continue
                for p in sorted(base.rglob("*")):
                    if p.is_file() and not p.name.startswith("."):
                        z.write(p, f"{name}/{p.relative_to(base)}")
                        counts["files"] += 1
    return counts


def backups_in(folder: Path) -> list[Path]:
    """This app's backups in `folder`, oldest first (only files it named itself; nothing else there is touched)."""
    try:
        found = [p for p in folder.iterdir() if p.is_file() and (_DATED.match(p.name) or p.name == f"{PREFIX}.zip")]
    except OSError:
        return []
    return sorted(found, key=lambda p: p.stat().st_mtime)


def run() -> dict:
    """Make a backup now, with the settings under Settings → Your data."""
    cfg = settings.get("backup")
    folder = check_folder(cfg.get("folder", ""))
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise BackupError(f"Couldn't create {_short(folder)}: {e.strerror or e}") from e
    overwrite = cfg.get("mode") == "overwrite"
    name = f"{PREFIX}.zip" if overwrite else f"{PREFIX}-{datetime.now():%Y-%m-%d_%H%M}.zip"
    final = folder / name
    part = folder / f".{name}.part"  # hidden and unfinished, so Nextcloud doesn't upload half a backup
    try:
        counts = write_zip(part)
        os.replace(part, final)
    except OSError as e:
        part.unlink(missing_ok=True)
        raise BackupError(f"Couldn't write the backup to {_short(folder)}: {e.strerror or e}") from e
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    removed = []
    keep = int(cfg.get("keep") or 0)
    if not overwrite and keep > 0:
        dated = [p for p in backups_in(folder) if _DATED.match(p.name)]
        for old in dated[:-keep]:
            old.unlink(missing_ok=True)
            removed.append(old.name)
    size = final.stat().st_size
    settings.update("state", {"last_backup_at": db.now(), "last_backup_file": str(final), "last_backup_bytes": size})
    return {"file": _short(final), "size": human_size(size), "files": counts["files"], "removed": removed}


def due(now: datetime | None = None) -> bool:
    """An automatic backup is due: switched on, a folder chosen, the last one long enough ago, and no failed try in
    the last few hours."""
    cfg = settings.get("backup")
    if not cfg.get("enabled") or not (cfg.get("folder") or "").strip():
        return False
    now = now or db.parse_ts(db.now())
    state = settings.get("state")
    last = db.parse_ts(state.get("last_backup_at"))
    if last and now - last < timedelta(days=max(1, int(cfg.get("every_days") or 7))) - timedelta(minutes=5):
        return False
    tried = db.parse_ts(state.get("backup_tried_at"))
    return not tried or now - tried >= timedelta(hours=RETRY_HOURS) or bool(last and tried <= last)


def mark_tried() -> None:
    settings.update("state", {"backup_tried_at": db.now()})


def status() -> dict:
    """What the Settings page shows about backups."""
    cfg = settings.get("backup")
    state = settings.get("state")
    out = {"cfg": cfg, "suggested": suggested_folder(), "modes": MODES, "every": EVERY, "problem": "",
           "last_at": state.get("last_backup_at") or "", "last_file": "", "last_size": "",
           "next_at": "", "count": 0, "total": ""}
    if state.get("last_backup_file"):
        out["last_file"] = _short(Path(state["last_backup_file"]))
        out["last_size"] = human_size(int(state.get("last_backup_bytes") or 0))
    folder_raw = (cfg.get("folder") or "").strip()
    if not folder_raw:
        return out
    try:
        folder = check_folder(folder_raw)
    except BackupError as e:
        out["problem"] = str(e)
        return out
    found = backups_in(folder) if folder.is_dir() else []
    out["count"], out["total"] = len(found), human_size(sum(p.stat().st_size for p in found))
    if cfg.get("enabled"):
        last = db.parse_ts(out["last_at"])
        nxt = last + timedelta(days=max(1, int(cfg.get("every_days") or 7))) if last else None
        if nxt and nxt > db.parse_ts(db.now()):
            out["next_at"] = nxt.strftime("%Y-%m-%dT%H:%M:%S")
        else:
            out["next_at"] = "now"  # due: made within a minute, or a few hours after a failed try
    return out


def human_size(n: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"
