"""Watched folders: keep your career database in step with folders that change over time.

* A folder on this Mac, e.g. the one the Nextcloud desktop app keeps in sync.
* A Nextcloud (or any WebDAV) folder directly: files are mirrored into the data folder first.

New files are imported. A changed file replaces its old version: its unchecked facts are taken out
and it's read again (facts you've marked as correct stay). Files deleted at the source are kept here.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, quote, unquote, urlparse

import httpx2

from . import config, db, ingest, keychain, retrieval, tasks

MAX_FILES = 3000
NS = {"d": "DAV:"}
PROPFIND_BODY = ('<?xml version="1.0" encoding="utf-8"?><d:propfind xmlns:d="DAV:"><d:prop>'
                 '<d:getlastmodified/><d:getetag/><d:getcontentlength/><d:resourcetype/></d:prop></d:propfind>')


class WatchError(Exception):
    """Scanning failed; the message is for the user."""


def secret_name(folder_id: int) -> str:
    return f"webdav-folder-{folder_id}"


def nextcloud_candidates() -> list[str]:
    """Where the Nextcloud desktop app usually syncs to on a Mac."""
    home = Path.home()
    found = []
    classic = home / "Nextcloud"
    if classic.is_dir():
        found.append(str(classic))
    cloud = home / "Library" / "CloudStorage"
    try:
        found += [str(p) for p in sorted(cloud.iterdir()) if p.name.lower().startswith("nextcloud") and p.is_dir()]
    except OSError:
        pass
    return found


def webdav_url(url: str, username: str) -> str:
    """Accept the address of a folder in Nextcloud's web interface and turn it into its WebDAV address."""
    url = url.strip()
    p = urlparse(url)
    if "/remote.php/" in p.path:
        return url.rstrip("/")
    folder = parse_qs(p.query).get("dir", [""])[0] if "/apps/files" in p.path else ""
    base = p.path.split("/index.php")[0].split("/apps/files")[0].rstrip("/")
    return f"{p.scheme}://{p.netloc}{base}/remote.php/dav/files/{quote(username)}{quote(folder.rstrip('/'))}"


def add_local(path: str) -> int:
    root = Path(path).expanduser()
    if not root.is_dir():
        raise WatchError(f"Not a folder: {root}")
    root = root.resolve()
    existing = db.q1("SELECT id FROM watched_folders WHERE path = ?", (str(root),))
    if existing:
        return existing["id"]
    return db.insert("watched_folders", {"kind": "local", "path": str(root), "created_at": db.now()})


def add_webdav(url: str, username: str, password: str) -> int:
    if not url.startswith(("https://", "http://")) or not username or not password:
        raise WatchError("Enter the Nextcloud address, your username and an app password.")
    dav = webdav_url(url, username)
    existing = db.q1("SELECT id FROM watched_folders WHERE path = ?", (dav,))
    folder_id = existing["id"] if existing else db.insert(
        "watched_folders", {"kind": "webdav", "path": dav, "username": username, "created_at": db.now()})
    keychain.set(secret_name(folder_id), password)
    return folder_id


def remove(folder_id: int) -> None:
    keychain.delete(secret_name(folder_id))
    db.ex("DELETE FROM watched_folders WHERE id = ?", (folder_id,))


def due() -> list[dict]:
    out = []
    for f in db.q("SELECT * FROM watched_folders WHERE enabled = 1"):
        last = db.parse_ts(f["last_scan_at"])
        if last is None or (db.parse_ts(db.now()) - last).total_seconds() >= f["interval_minutes"] * 60:
            out.append(f)
    return out


# ---------------------------------------------------------------------------------------
# Local folders
# ---------------------------------------------------------------------------------------

def _iter_files(root: Path):
    from . import knowledge

    count = 0
    roots = knowledge.skip_roots()
    if knowledge.is_skipped_dir(root, roots):  # the agent folder holds summaries, not your documents
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in ingest.SKIP_DIRS
                             and not d.lower().endswith(".mbox")
                             and not knowledge.is_skip_point(Path(dirpath) / d, roots))
        for name in sorted(filenames):
            if name.startswith((".", "~$")):
                continue
            p = Path(dirpath) / name
            if p.suffix.lower() in ingest.SUPPORTED and p.suffix.lower() != ".mbox":
                yield p
                count += 1
                if count >= MAX_FILES:
                    return


def _replace(doc_id: int, path: Path, data: bytes, sha: str, mtime: float, watch_id: int) -> None:
    text = ingest.clean_text(ingest.extract_text(path, data))
    if len(text) < 20:
        raise ingest.ImportErrorUser("No readable text found.")
    db.update("documents", doc_id, {"text": text, "sha256": sha, "word_count": len(text.split()), "source_mtime": mtime,
                                    "status": "new", "error": None, "analyzed_at": None, "watch_id": watch_id})
    db.ex("DELETE FROM facts WHERE document_id = ? AND verified = 0 AND source = 'extracted'", (doc_id,))
    retrieval.index_document(doc_id)


def scan_local(root: Path, watch_id: int) -> dict:
    new, changed, errors = [], [], []
    seen = unchanged = 0
    for p in _iter_files(root):
        seen += 1
        try:
            st = p.stat()
            if st.st_size > ingest.MAX_FILE_BYTES:
                continue
            doc = db.q1("SELECT id, sha256, source_mtime FROM documents WHERE source_path = ?", (str(p),))
            if doc and doc["source_mtime"] == st.st_mtime:
                unchanged += 1
                continue
            data = p.read_bytes()
            sha = hashlib.sha256(data).hexdigest()
            if doc:
                if doc["sha256"] == sha:
                    db.update("documents", doc["id"], {"source_mtime": st.st_mtime, "watch_id": watch_id})
                    unchanged += 1
                    continue
                try:
                    _replace(doc["id"], p, data, sha, st.st_mtime, watch_id)
                    changed.append(doc["id"])
                except sqlite3.IntegrityError:  # identical to another document already imported
                    unchanged += 1
                continue
            twin = db.q1("SELECT id, source_path FROM documents WHERE sha256 = ?", (sha,))
            if twin:  # same file imported earlier (e.g. uploaded): adopt it so future edits are tracked
                if not twin["source_path"] or not Path(twin["source_path"]).exists():
                    db.update("documents", twin["id"], {"source_path": str(p), "source_mtime": st.st_mtime, "watch_id": watch_id})
                unchanged += 1
                continue
            doc_id, created = ingest.import_path(p)
            db.update("documents", doc_id, {"source_mtime": st.st_mtime, "watch_id": watch_id})
            if created:
                new.append(doc_id)
        except ingest.ImportErrorUser as e:
            errors.append(f"{p.name}: {e}")
        except PermissionError:
            errors.append(f"{p.name}: macOS didn't allow reading it")
        except Exception as e:  # noqa: BLE001 - one bad file mustn't stop the scan
            errors.append(f"{p.name}: couldn't read ({e.__class__.__name__})")
    return {"seen": seen, "new": new, "changed": changed, "unchanged": unchanged, "errors": errors}


# ---------------------------------------------------------------------------------------
# Nextcloud / WebDAV
# ---------------------------------------------------------------------------------------

def _propfind(client: httpx2.Client, url: str) -> list[dict]:
    try:
        r = client.request("PROPFIND", url, headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
                           content=PROPFIND_BODY)
    except httpx2.HTTPError as e:
        raise WatchError(f"Couldn't reach the server ({e.__class__.__name__}).") from e
    if r.status_code == 401:
        raise WatchError("The server rejected the username or app password.")
    if r.status_code == 404:
        raise WatchError("Folder not found on the server. Check the address.")
    if r.status_code >= 400:
        raise WatchError(f"The server answered HTTP {r.status_code}.")
    try:
        root = ET.fromstring(r.content)
    except ET.ParseError as e:
        raise WatchError("The server's answer wasn't WebDAV. Check the address.") from e
    items = []
    for resp in root.findall("d:response", NS):
        prop = None
        for ps in resp.findall("d:propstat", NS):
            if " 200 " in f" {ps.findtext('d:status', default='', namespaces=NS)} ":
                prop = ps.find("d:prop", NS)
        if prop is None:
            continue
        size = prop.findtext("d:getcontentlength", default="0", namespaces=NS) or "0"
        items.append({
            "href": resp.findtext("d:href", default="", namespaces=NS),
            "dir": prop.find("d:resourcetype/d:collection", NS) is not None,
            "etag": (prop.findtext("d:getetag", default="", namespaces=NS) or "").strip('"'),
            "modified": prop.findtext("d:getlastmodified", default="", namespaces=NS),
            "size": int(size) if size.isdigit() else 0,
        })
    return items


def sync_webdav(folder: dict) -> Path:
    """Mirror the remote folder's documents into the data folder. Returns the mirror folder."""
    base = folder["path"].rstrip("/") + "/"
    parsed = urlparse(base)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    base_path = unquote(parsed.path)
    mirror = (config.DATA_DIR / "mirror" / str(folder["id"])).resolve()
    mirror.mkdir(parents=True, exist_ok=True)
    known = {r["href"]: r for r in db.q("SELECT * FROM remote_files WHERE folder_id = ?", (folder["id"],))}
    password = keychain.get(secret_name(folder["id"]))
    if not password:
        raise WatchError("The app password for this Nextcloud folder is missing. Remove the folder and add it again.")
    files = 0
    with httpx2.Client(auth=(folder["username"] or "", password), timeout=60, follow_redirects=True,
                       headers={"User-Agent": "CareerAgent/1.0"}) as client:
        queue, seen_dirs = [base], {base_path.rstrip("/")}
        while queue and files < MAX_FILES:
            url = queue.pop(0)
            here = unquote(urlparse(url).path).rstrip("/")
            for it in _propfind(client, url):
                href_url = it["href"] if it["href"].startswith(("http://", "https://")) else origin + it["href"]
                path = unquote(urlparse(href_url).path)
                if path.rstrip("/") == here:
                    continue  # the folder itself
                name = PurePosixPath(path.rstrip("/")).name
                if name.startswith("."):
                    continue
                if it["dir"]:
                    if path.rstrip("/") not in seen_dirs and path.startswith(base_path):
                        seen_dirs.add(path.rstrip("/"))
                        queue.append(href_url.rstrip("/") + "/")
                    continue
                if PurePosixPath(name).suffix.lower() not in ingest.SUPPORTED or name.lower().endswith(".mbox"):
                    continue
                if it["size"] > ingest.MAX_FILE_BYTES or not path.startswith(base_path):
                    continue
                local = (mirror / path[len(base_path):]).resolve()
                if mirror not in local.parents:
                    continue  # never write outside the mirror folder
                files += 1
                prev = known.get(it["href"])
                if prev and prev["etag"] == it["etag"] and it["etag"] and local.exists():
                    continue
                r = client.get(href_url)
                if r.status_code >= 400:
                    continue
                local.parent.mkdir(parents=True, exist_ok=True)
                local.write_bytes(r.content)
                if it["modified"]:
                    try:
                        ts = parsedate_to_datetime(it["modified"]).timestamp()
                        os.utime(local, (ts, ts))
                    except (TypeError, ValueError, OSError):
                        pass
                # The remote copy changed: make the next scan re-read it even if the timestamp didn't move.
                db.ex("UPDATE documents SET source_mtime = NULL WHERE source_path = ?", (str(local),))
                db.ex("INSERT INTO remote_files (folder_id, href, etag, local_path) VALUES (?, ?, ?, ?) "
                      "ON CONFLICT(folder_id, href) DO UPDATE SET etag = excluded.etag, local_path = excluded.local_path",
                      (folder["id"], it["href"], it["etag"], str(local)))
    return mirror


# ---------------------------------------------------------------------------------------

def scan(folder_id: int) -> dict:
    folder = db.q1("SELECT * FROM watched_folders WHERE id = ?", (folder_id,))
    if not folder:
        return {"skipped": "folder removed"}
    db.update("watched_folders", folder_id, {"last_scan_at": db.now()})
    try:
        if folder["kind"] == "webdav":
            root = sync_webdav(folder)
        else:
            root = Path(folder["path"])
            if not root.is_dir():
                raise WatchError("Folder not found. Is the disk connected and the folder still there?")
        result = scan_local(root, folder_id)
    except PermissionError as e:
        err = "macOS didn't allow reading this folder. Allow access when asked, or give the app Full Disk Access."
        db.update("watched_folders", folder_id, {"last_status": "error", "last_error": err})
        raise WatchError(err) from e
    except WatchError as e:
        db.update("watched_folders", folder_id, {"last_status": "error", "last_error": str(e)})
        raise
    for doc_id in ingest.cv_first(result["new"] + result["changed"]):
        tasks.enqueue("analyze_document", {"doc_id": doc_id}, ref=f"doc:{doc_id}")
    if result["new"] or result["changed"]:
        tasks.enqueue("embed_index", {}, ref="index")
    status = (f"{result['seen']} documents: {len(result['new'])} new, {len(result['changed'])} changed"
              + (f", {len(result['errors'])} couldn't be read" if result["errors"] else ""))
    db.update("watched_folders", folder_id, {"last_status": status, "files_seen": result["seen"],
                                             "last_error": "; ".join(result["errors"][:5]) or None})
    return {k: (len(v) if isinstance(v, list) else v) for k, v in result.items()}
