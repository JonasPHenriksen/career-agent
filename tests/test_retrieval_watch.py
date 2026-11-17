"""Retrieval (chunks, BM25, embeddings via a fake Ollama), watched folders and Nextcloud/WebDAV sync."""
from __future__ import annotations

import base64
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np
import pytest

from career_agent import db, ingest, keychain, profile, retrieval, settings, tracker, watch, writer

from .conftest import CV_TEXT, JOB_TEXT


# ---------- a fake Ollama embedding endpoint: bag-of-words vectors, good enough to test ranking ----------

VOCAB = ["scrum", "betaling", "payment", "sql", "budget", "novo", "danske", "team", "bage", "kage"]


def _vec(text: str) -> list[float]:
    t = text.lower()
    v = np.array([t.count(w) for w in VOCAB] + [1e-3], dtype=np.float32)
    return (v / np.linalg.norm(v)).tolist()


class _FakeOllama(BaseHTTPRequestHandler):
    calls = 0

    def do_GET(self):  # noqa: N802
        self._json({"models": [{"name": "bge-m3:latest"}]})

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        _FakeOllama.calls += 1
        self._json({"embeddings": [_vec(x) for x in body["input"]]})

    def _json(self, data):
        raw = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


@pytest.fixture()
def ollama(fake):
    httpd = HTTPServer(("127.0.0.1", 0), _FakeOllama)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    settings.update("llm", {"ollama_url": f"http://127.0.0.1:{httpd.server_port}"})
    retrieval.embeddings_available(force=True)
    yield httpd
    httpd.shutdown()
    retrieval._avail.update(at=0.0, ok=False, key=None)


def test_chunking_and_keyword_search_without_embeddings(fake):
    settings.update("llm", {"ollama_url": "http://127.0.0.1:9"})  # nothing listening: keyword search only
    retrieval.embeddings_available(force=True)
    long_text = "\n\n".join(f"Afsnit {n}. " + ("Vi arbejdede med betalinger og Scrum i teamet. " * 12) for n in range(10))
    chunks = retrieval.split_chunks(long_text)
    assert len(chunks) > 3 and all(len(c) <= retrieval.CHUNK_HARD_MAX for c in chunks)
    doc_id, _ = ingest.import_bytes("cv.txt", CV_TEXT.encode())
    assert db.scalar("SELECT COUNT(*) FROM chunks WHERE document_id = ?", (doc_id,)) >= 1, "imports are split into passages"
    hits = retrieval.search_chunks("Copenhagen Business School uddannelse", k=1)
    assert hits and "Copenhagen Business School" in hits[0]["text"]
    assert retrieval.status()["available"] is False


def test_hybrid_ranking_and_job_relevant_digest(ollama):
    for k in range(40):
        db.insert("facts", {"category": "project", "title": f"Bagte kage til {k} kollegaer", "detail": "kage bage",
                            "created_at": db.now()})
    db.insert("facts", {"category": "project", "title": "Scrum team for payments", "detail": "SQL og betaling i Danske Bank",
                        "created_at": db.now()})
    db.insert("facts", {"category": "experience", "title": "Product Owner", "organization": "Danske Bank", "created_at": db.now()})
    facts = db.q("SELECT * FROM facts WHERE category = 'project'")
    order = retrieval.rank_facts("Erfaring med Scrum, SQL og betalinger", facts)
    best = db.q1("SELECT id FROM facts WHERE title = 'Scrum team for payments'")["id"]
    assert order[0] == best
    digest = profile.facts_digest(1200, query="Erfaring med Scrum, SQL og betalinger")
    assert f"[#{best}]" in digest, "with a tight budget, the fact relevant to the job is kept"
    assert "Product Owner" in digest and "less relevant to this job" in digest
    assert retrieval.embed_pending()["facts"] == 42
    calls = _FakeOllama.calls
    retrieval.embed_pending()
    assert _FakeOllama.calls == calls, "unchanged facts aren't embedded again"


def test_long_documents_are_read_in_parts(fake, monkeypatch):
    parts = profile.analysis_parts(("Linje med tekst om projekter. " * 40 + "\n\n") * 30, 4000)
    assert len(parts) > 1 and all(len(p) <= 4000 for p in parts)
    doc_id, _ = ingest.import_bytes("lang rapport.txt", (CV_TEXT + "\n\n" + ("Fyld. " * 9000)).encode())
    real = profile._limits
    monkeypatch.setattr(profile, "_limits", lambda: {**real(), "doc_chars": 20000})  # force several parts
    profile.analyze_document(doc_id)
    calls = [c for c in fake.calls if c[2] is profile.ANALYZE_SCHEMA]
    assert len(calls) >= 3 and "part 1 of" in calls[0][1]


def test_fact_check_finds_evidence_in_documents(fake):
    doc_id, _ = ingest.import_bytes("cv.txt", CV_TEXT.encode())
    profile.analyze_document(doc_id)
    job_id = __import__("career_agent").jobs.add_from_text(JOB_TEXT)
    __import__("career_agent").jobs.parse(job_id, use_text=True)
    app_id = __import__("career_agent").jobs.create_application(job_id)
    writer.draft(app_id)
    claims = db.jload(tracker.app_with_job(app_id)["claims"])
    sourced = [c for c in claims["claims"] if c.get("source")]
    assert len(sourced) == 1, "only the claim the CV really backs gets a source"
    assert "8 udviklere" in sourced[0]["text"]
    assert sourced[0]["source"]["document_id"] == doc_id and sourced[0]["status"] == "supported"
    assert any(c["status"] == "unsupported" and "12 udviklere" in c["text"] for c in claims["claims"])


def test_watched_local_folder_tracks_new_and_changed_files(fake, tmp_path):
    folder = tmp_path / "Nextcloud" / "Jobs"
    folder.mkdir(parents=True)
    (folder / "cv.txt").write_text(CV_TEXT)
    (folder / ".hidden.txt").write_text("secret " * 20)
    (folder / "~$lock.docx").write_text("x")
    wid = watch.add_local(str(folder))
    first = watch.scan(wid)
    assert first["new"] == 1 and first["seen"] == 1
    assert watch.scan(wid)["unchanged"] == 1, "unchanged files are skipped"
    doc = db.q1("SELECT * FROM documents WHERE source_path = ?", (str(folder / "cv.txt"),))
    profile.analyze_document(doc["id"])
    keep = db.q1("SELECT id FROM facts WHERE document_id = ? AND category = 'education'", (doc["id"],))["id"]
    db.update("facts", keep, {"verified": 1})
    time.sleep(0.01)
    (folder / "cv.txt").write_text(CV_TEXT.replace("Scrum, Jira", "Scrum, Jira, Kubernetes"))
    os.utime(folder / "cv.txt", (time.time() + 5, time.time() + 5))
    (folder / "ansoegning.txt").write_text("Kære Mette\n\nJeg søger stillingen. " * 10)
    third = watch.scan(wid)
    assert third["changed"] == 1 and third["new"] == 1
    assert db.scalar("SELECT COUNT(*) FROM documents") == 2, "a changed file replaces its old version"
    updated = db.q1("SELECT * FROM documents WHERE id = ?", (doc["id"],))
    assert "Kubernetes" in updated["text"] and updated["status"] == "new"
    assert db.q1("SELECT id FROM facts WHERE id = ?", (keep,)), "facts you checked survive a re-read"
    assert db.scalar("SELECT COUNT(*) FROM facts WHERE document_id = ? AND verified = 0", (doc["id"],)) == 0
    assert db.q1("SELECT * FROM tasks WHERE kind = 'analyze_document' AND payload LIKE ?", (f'%"doc_id": {doc["id"]}%',))


def test_nextcloud_web_address_becomes_webdav():
    assert watch.webdav_url("https://cloud.example.com/apps/files/?dir=/Job%20stuff&fileid=12", "jonas") == \
        "https://cloud.example.com/remote.php/dav/files/jonas/Job%20stuff"
    assert watch.webdav_url("https://cloud.example.com/index.php/apps/files/?dir=/Jobs", "jonas") == \
        "https://cloud.example.com/remote.php/dav/files/jonas/Jobs"
    assert watch.webdav_url("https://x.org/nc/remote.php/dav/files/me/A/", "me") == "https://x.org/nc/remote.php/dav/files/me/A"


class _FakeDav(BaseHTTPRequestHandler):
    files = {"/remote.php/dav/files/jonas/Jobs/cv.txt": CV_TEXT.encode(),
             "/remote.php/dav/files/jonas/Jobs/Ansøgninger/lunar.txt": ("Kære Mette\n\nJeg søger. " * 10).encode(),
             "/remote.php/dav/files/jonas/Jobs/photo.jpg": b"\xff\xd8"}
    etags = {}
    gets = 0

    def _ok(self):
        expected = "Basic " + base64.b64encode(b"jonas:app-pass").decode()
        if self.headers.get("Authorization") != expected:
            self.send_response(401)
            self.end_headers()
            return False
        return True

    def do_PROPFIND(self):  # noqa: N802
        if not self._ok():
            return
        from urllib.parse import quote, unquote
        path = unquote(self.path).rstrip("/") + "/"
        entries = [(path, True, "")]
        children = set()
        for f in self.files:
            if f.startswith(path):
                rest = f[len(path):]
                children.add((path + rest.split("/")[0] + ("/" if "/" in rest else ""), "/" in rest))
        entries += [(c, is_dir, "") for c, is_dir in sorted(children)]
        out = ['<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">']
        for href, is_dir, _ in entries:
            rtype = "<d:collection/>" if is_dir else ""
            etag = "" if is_dir else f"<d:getetag>&quot;{self.etags.get(href, 'v1')}&quot;</d:getetag>"
            size = "" if is_dir else f"<d:getcontentlength>{len(self.files[href])}</d:getcontentlength>"
            out.append(f"<d:response><d:href>{quote(href)}</d:href><d:propstat><d:prop><d:resourcetype>{rtype}</d:resourcetype>"
                       f"{etag}{size}<d:getlastmodified>Mon, 09 Nov 2026 10:00:00 GMT</d:getlastmodified></d:prop>"
                       f"<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>")
        out.append("</d:multistatus>")
        raw = "".join(out).encode()
        self.send_response(207)
        self.send_header("content-type", "application/xml")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802
        if not self._ok():
            return
        from urllib.parse import unquote
        _FakeDav.gets += 1
        raw = self.files[unquote(self.path)]
        self.send_response(200)
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


def test_nextcloud_webdav_sync(fake):
    httpd = HTTPServer(("127.0.0.1", 0), _FakeDav)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{httpd.server_port}"
        with pytest.raises(watch.WatchError):
            watch.scan(watch.add_webdav(f"{base}/apps/files/?dir=/Jobs", "jonas", "wrong"))
        wid = watch.add_webdav(f"{base}/apps/files/?dir=/Jobs", "jonas", "app-pass")
        assert keychain.get(watch.secret_name(wid)) == "app-pass"
        result = watch.scan(wid)
        assert result["new"] == 2, "documents in sub-folders are fetched; photos are skipped"
        titles = {r["filename"] for r in db.q("SELECT filename FROM documents")}
        assert titles == {"cv.txt", "lunar.txt"}
        gets = _FakeDav.gets
        assert watch.scan(wid)["new"] == 0 and _FakeDav.gets == gets, "unchanged files aren't downloaded again"
        _FakeDav.files["/remote.php/dav/files/jonas/Jobs/cv.txt"] = (CV_TEXT + "\nNyt kursus i SQL 2026").encode()
        _FakeDav.etags["/remote.php/dav/files/jonas/Jobs/cv.txt"] = "v2"
        assert watch.scan(wid)["changed"] == 1
        watch.remove(wid)
        assert not keychain.get(watch.secret_name(wid))
    finally:
        httpd.shutdown()
