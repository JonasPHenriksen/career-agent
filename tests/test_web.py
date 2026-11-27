from __future__ import annotations

from fastapi.testclient import TestClient

from career_agent import db, jobs, profile, tracker, writer
from career_agent.web.app import app

from .conftest import CV_TEXT, JOB_TEXT, approve_waiting


def _seed():
    from career_agent import ingest

    doc_id, _ = ingest.import_bytes("cv.txt", CV_TEXT.encode())
    profile.analyze_document(doc_id)
    approve_waiting()
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
             "/tasks", "/tasks/indicator", f"/documents/{doc_id}", "/history", "/history?show=deleted"]
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


def test_not_for_me_on_a_draft_removes_it_and_offers_undo(fake):
    job = db.insert("jobs", {"url": "https://x.example/1", "title": "Product Owner", "company_name": "FinPay",
                             "status": "shortlisted", "parse_status": "parsed", "created_at": db.now()})
    app_id = db.insert("applications", {"job_id": job, "status": "draft", "created_at": db.now(), "updated_at": db.now()})
    c = TestClient(app)
    for url in ("/tracker", f"/applications/{app_id}", "/jobs?view=applying", f"/jobs/{job}"):
        assert "removes your draft" in c.get(url).text or "remove your draft" in c.get(url).text, url
    r = c.post(f"/jobs/{job}/status", data={"status": "dismissed", "next": "/tracker"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/tracker?") and "undo=" in r.headers["location"]
    assert "your draft was removed" in r.headers["location"].replace("+", " ").replace("%20", " ")
    assert not db.q1("SELECT 1 FROM applications WHERE id = ?", (app_id,)) and jobs.get(job)["status"] == "dismissed"
    assert c.get("/tracker").status_code == 200
    op = r.headers["location"].split("undo=")[1].split("&")[0].split("#")[0]
    c.post(f"/history/{op}/undo")
    assert db.q1("SELECT status FROM applications WHERE id = ?", (app_id,))["status"] == "draft"
    assert jobs.get(job)["status"] == "shortlisted"


def test_jobs_the_app_could_not_read_say_so_and_point_to_the_paste_box(fake):
    def job(n, **kw):
        return db.insert("jobs", {"url": f"https://x.example/{n}", "title": f"Job {n}", "company_name": "FinPay",
                                  "status": "new", "created_at": db.now(), **kw})
    locked = job(1, parse_status="skipped", link_locked=1, notes="Collected from Jobnet. The site behind the link doesn't let the app open it.")
    broken = job(2, parse_status="error", parse_error="This page doesn't look like a single job ad. Open the specific ad, or paste its text.")
    busy = job(3, parse_status="error", parse_error="The model is overloaded right now.")  # tried again by itself
    job(4, parse_status="skipped", status="filtered", notes="Filtered out by quick screening.")  # set aside: not yours to fix
    job(5, parse_status="error", parse_error="This page doesn't look like a single job ad.", fit_score=40)  # read before
    assert [j["id"] for j in jobs.needing_text()] == [broken, locked]
    c = TestClient(app)
    inbox = c.get("/jobs").text
    assert "2 jobs the app couldn't read." in inbox and 'href="/jobs?view=needs-text"' in inbox
    assert inbox.count(">needs the ad text</a>") == 2 and f'href="/jobs/{locked}#paste"' in inbox
    assert f'let the app open it. <a href="/jobs/{locked}#paste">Paste it' in inbox and "couldn't read, trying again" in inbox
    only = c.get("/jobs?view=needs-text").text
    assert "Job 1" in only and "Job 2" in only and "Job 3" not in only and "Job 4" not in only and "Job 5" not in only
    assert "2 jobs the app couldn't read." not in only, "no notice on the list itself"
    assert 'id="paste"' in c.get(f"/jobs/{locked}").text and 'id="paste"' in c.get(f"/jobs/{broken}").text
    assert "job</span>" not in c.get("/").text and "jobs the app couldn't read: paste the ad text" in c.get("/").text
    for j in (locked, broken):
        db.update("jobs", j, {"parse_status": "parsed"})
    assert "Needs the ad text" not in c.get("/jobs").text, "the tab goes away when there's nothing to paste"


def test_a_sent_application_and_its_job_cannot_be_removed(fake):
    _, job_id, app_id = _seed()
    c = TestClient(app)
    note = tracker.KEPT_NOTE.replace("'", "&#39;")
    page = c.get(f"/jobs/{job_id}").text
    assert f'aria-disabled="true" title="{note}">Delete job</button>' in page
    assert f'aria-disabled="true" title="{note}">Not for me</button>' in page
    assert f'action="/jobs/{job_id}/delete"' not in page
    assert f'aria-label="Not for me">✕</button>' in c.get("/jobs?view=all").text
    assert f'title="{note}">Delete application</button>' in c.get(f"/applications/{app_id}?tab=history").text

    r = c.post(f"/jobs/{job_id}/delete", follow_redirects=False)
    assert r.status_code == 303 and "err=" in r.headers["location"] and jobs.get(job_id)
    c.post(f"/jobs/{job_id}/status", data={"status": "dismissed"}, follow_redirects=False)
    assert jobs.get(job_id)["status"] != "dismissed"
    c.post(f"/applications/{app_id}/delete", follow_redirects=False)
    assert db.q1("SELECT 1 FROM applications WHERE id = ?", (app_id,)), "the guard is on the server too"

    for e in tracker.timeline(app_id):
        tracker.delete_event(app_id, e["id"])
    assert f'action="/applications/{app_id}/delete"' in c.get(f"/applications/{app_id}?tab=history").text
    c.post(f"/applications/{app_id}/delete", follow_redirects=False)
    assert not db.q1("SELECT 1 FROM applications WHERE id = ?", (app_id,)), "back to a draft: it can go"
