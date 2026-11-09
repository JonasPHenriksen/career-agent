from __future__ import annotations

from fastapi.testclient import TestClient

from career_agent import db, jobs, profile, tracker, writer
from career_agent.web.app import app

from .conftest import CV_TEXT, JOB_TEXT


def _seed():
    from career_agent import ingest

    doc_id, _ = ingest.import_bytes("cv.txt", CV_TEXT.encode())
    profile.analyze_document(doc_id)
    job_id = jobs.add_from_text(JOB_TEXT)
    jobs.parse(job_id, use_text=True)
    jobs.score(job_id)
    app_id = db.q1("SELECT id FROM applications WHERE job_id = ?", (job_id,))["id"]
    writer.draft(app_id)
    tracker.add_event(app_id, "applied")
    return doc_id, job_id, app_id


def test_every_page_renders(fake):
    doc_id, job_id, app_id = _seed()
    company_id = jobs.get(job_id)["company_id"]
    c = TestClient(app)
    pages = ["/", "/jobs", "/jobs?view=all&sort=deadline", "/jobs?view=filtered", f"/jobs/{job_id}",
             "/tracker", "/companies", "/companies?show=all", f"/companies/{company_id}", "/insights", "/settings",
             "/tasks", "/tasks/indicator", f"/documents/{doc_id}"]
    pages += [f"/profile?tab={t}" for t in ("about", "documents", "facts", "style")]
    pages += [f"/applications/{app_id}?tab={t}" for t in ("letter", "cv", "check", "voice", "send", "timeline", "interview", "history")]
    for url in pages:
        r = c.get(url)
        assert r.status_code == 200, f"{url} -> {r.status_code}\n{r.text[:2000]}"
    r = c.get(f"/applications/{app_id}?tab=check")
    assert 'claim-unsupported' in r.text, "unsupported claims are highlighted in the letter"


def test_forms_and_exports(fake):
    doc_id, job_id, app_id = _seed()
    c = TestClient(app)
    r = c.post("/profile/about", data={"name": "Sara Lund", "headline": "PO", "mode_remote": "1"}, follow_redirects=False)
    assert r.status_code == 303
    assert c.get("/profile?tab=about").text.count('value="PO"') == 1
    r = c.post(f"/applications/{app_id}/save", data={"subject": "S", "letter": "Kære Mette\n\nNy tekst her med mange ord."}, follow_redirects=False)
    assert r.status_code == 303 and db.q1("SELECT letter FROM applications WHERE id = ?", (app_id,))["letter"].startswith("Kære Mette")
    for name in ("letter.pdf", "cv.pdf", "letter.docx", "cv.docx"):
        r = c.get(f"/applications/{app_id}/export/{name}")
        assert r.status_code == 200 and len(r.content) > 1000, name
    r = c.post("/jobs/add", data={"url": "", "text": JOB_TEXT + " extra"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/jobs/")
    r = c.post(f"/applications/{app_id}/event", data={"type": "interview_invite", "date": "2026-11-01", "summary": "Call"}, follow_redirects=False)
    assert r.status_code == 303 and tracker.app_with_job(app_id)["status"] == "interview"
    r = c.post("/facts/add", data={"category": "skill", "title": "Python"}, follow_redirects=False)
    assert r.status_code == 303 and db.q1("SELECT * FROM facts WHERE title = 'Python'")["verified"] == 1
    r = c.get("/settings/backup")
    assert r.status_code == 200 and r.content[:2] == b"PK"


def test_security_guards(fake):
    c = TestClient(app)
    assert c.get("/", headers={"host": "evil.example.com"}).status_code == 403
    r = c.post("/profile/about", data={"name": "x"}, headers={"origin": "https://evil.example.com"})
    assert r.status_code == 403
