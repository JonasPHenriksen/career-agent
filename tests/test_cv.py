"""The CV as data: your own CV read into a base, small changes per job, typeset in the app, exported as PDF and JSON."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from career_agent import cvdata, cvrender, db, exporter, jobs, profile, settings, tracker, writer
from career_agent.web.app import app

from .conftest import JOB_TEXT
from .test_pipeline import _import_docs


def _draft() -> dict:
    cv_id, letter_id = _import_docs()
    profile.analyze_document(cv_id)
    profile.analyze_document(letter_id)
    settings.update("automation", {"auto_draft": False})
    job_id = jobs.add_from_text(JOB_TEXT)
    jobs.parse(job_id, use_text=True)
    app_id = jobs.create_application(job_id, draft=False)
    writer.draft(app_id)
    return tracker.app_with_job(app_id)


def test_your_cv_is_read_into_a_base_without_contact_details(fake):
    cv_id, _ = _import_docs()
    profile.analyze_document(cv_id)
    settings.update("profile", {"phone": "+45 12 34 56 78", "linkedin": "https://www.linkedin.com/in/sara-lund"})
    db.ex("UPDATE documents SET text = text || ? WHERE id = ?", ("\nsara@example.com · +45 12 34 56 78 · linkedin.com/in/sara-lund", cv_id))
    assert cvdata.base() is None and [d["id"] for d in cvdata.cv_documents()] == [cv_id]

    result = cvdata.build_base(cv_id)  # the fake model fails the test if it is shown the email address

    sent = fake.calls[-1][1]
    assert "12 34 56 78" not in sent and "sara-lund" not in sent and "Danske Bank" in sent
    base = cvdata.base()
    assert base["id"] == result["base_id"] == settings.get("cv")["base_id"] and base["language"] == "da"
    assert [w["name"] for w in base["data"]["work"]] == ["Danske Bank", "Novo Nordisk"]
    cvdata.build_base(cv_id)
    assert len(cvdata.bases()) == 1, "reading the same CV again replaces the earlier reading"


def test_a_tailored_cv_is_your_base_with_small_changes(fake):
    a = _draft()
    base, data = cvdata.base()["data"], db.jload(a["cv_data"])
    assert [(w["name"], w["position"], w["start"], w["end"]) for w in data["work"]] == \
           [(w["name"], w["position"], w["start"], w["end"]) for w in base["work"]], "employers, titles and dates stay"
    assert data["work"][0]["highlights"] == list(reversed(base["work"][0]["highlights"])), "only the order changed"
    assert "### Product Owner — Danske Bank" in a["cv"] and "## Erfaring" in a["cv"], "the text view follows the data"
    assert any(n.startswith("CV: Put the support result first") for n in db.jload(a["writer_notes"]))
    assert any(c["where"] == "cv" for c in db.jload(a["claims"])["claims"]), "the CV's lines are fact-checked too"

    writer.revise(a["id"], "Shorter bullets.", "cv")
    after = tracker.app_with_job(a["id"])
    assert len(db.jload(after["cv_data"])["work"][0]["highlights"]) == 1
    assert db.q1("SELECT cv_data FROM app_versions WHERE application_id = ? ORDER BY id DESC", (a["id"],))["cv_data"]


def test_without_a_cv_of_yours_one_is_written_from_the_facts(fake):
    profile.save_facts(None, [{"category": "experience", "title": "Product Owner", "organization": "Danske Bank"}])
    job_id = jobs.add_from_text(JOB_TEXT)
    jobs.parse(job_id, use_text=True)
    app_id = jobs.create_application(job_id, draft=False)
    writer.draft(app_id)
    a = tracker.app_with_job(app_id)
    assert a["cv_data"] is None and a["cv"].startswith("# ")
    assert exporter.render(a, "cv", "pdf")[0].startswith(b"%PDF")


def test_the_cv_is_typeset_with_your_details_and_design(fake):
    a = _draft()
    data = db.jload(a["cv_data"])
    m = cvrender.model(data, "da")
    assert m["header"]["name"] == "Sara Lund" and m["header"]["lines"][0] == {"text": "sara@example.com", "url": "mailto:sara@example.com"}
    assert [s["title"] for s in m["main"]] == ["Erfaring", "Uddannelse"] and m["main"][0]["entries"][0]["d"] == "2022 – nu"
    assert [s["title"] for s in m["sidebar"]] == ["Kompetencer", "Sprog"], "the base's own headings, else the language's"
    folder = cvrender.render(data, "da")
    assert (folder / "cv.pdf").read_bytes().startswith(b"%PDF") and (folder / "page-1.png").read_bytes()[1:4] == b"PNG"
    assert exporter.render(a, "cv", "pdf")[0] == (folder / "cv.pdf").read_bytes(), "the export is the typeset CV"

    settings.update("cv", {"primary": "#aa2200", "main_order": ["education", "work"]})
    m = cvrender.model(data, "da")
    assert m["design"]["primary"] == "#aa2200" and m["design"]["tint"] == "#eed3cc" and [s["title"] for s in m["main"]] == ["Uddannelse", "Erfaring"]
    assert cvrender.render(data, "da") != folder, "a new design is a new rendering"


def test_json_resume_export_and_dates():
    assert [cvdata.iso_date(x) for x in ("May 2026", "maj 2026", "2022", "Present", "nu", "2024-08", "Dec. 2024")] == \
           ["2026-05", "2026-05", "2022", "", "", "2024-08", "2024-12"]
    data = cvdata.clean({"label": "Engineer", "work": [{"name": "Acme", "position": "Dev", "start": "Jul 2024", "end": "Present",
                                                       "highlights": ["Built things", ""]}], "interests": ["Chess"]})
    out = cvdata.to_json_resume(data, {"name": "Sara Lund", "email": "sara@example.com", "linkedin": "https://linkedin.com/in/x"})
    assert out["basics"] == {"name": "Sara Lund", "label": "Engineer", "email": "sara@example.com",
                             "profiles": [{"network": "LinkedIn", "url": "https://linkedin.com/in/x"}]}
    assert out["work"] == [{"name": "Acme", "position": "Dev", "startDate": "2024-07", "highlights": ["Built things"]}]
    assert out["interests"] == [{"name": "Chess"}] and "education" not in out


def test_cv_pages_in_the_app(fake):
    a = _draft()
    c = TestClient(app)
    page = c.get(f"/applications/{a['id']}?tab=cv")
    assert page.status_code == 200 and f"/applications/{a['id']}/cv-page/1.png" in page.text and "Download JSON" in page.text
    assert c.get(f"/applications/{a['id']}/cv-page/1.png").headers["content-type"] == "image/png"
    assert c.get(f"/applications/{a['id']}/cv-page/9.png").status_code == 404
    assert c.get(f"/applications/{a['id']}/export/cv.pdf").content.startswith(b"%PDF")
    assert c.get(f"/applications/{a['id']}/export/cv.json").json()["work"][0]["name"] == "Danske Bank"

    r = c.post(f"/applications/{a['id']}/save", data={"cv_data": "{not json"}, follow_redirects=False)
    assert "nothing+was+saved" in r.headers["location"] and tracker.app_with_job(a["id"])["cv_data"] == a["cv_data"]
    data = db.jload(a["cv_data"])
    data["work"] = data["work"][:1]
    c.post(f"/applications/{a['id']}/save", data={"cv_data": json.dumps(data)}, follow_redirects=False)
    edited = tracker.app_with_job(a["id"])
    assert len(db.jload(edited["cv_data"])["work"]) == 1 and "Novo Nordisk" not in edited["cv"]
    version = db.q1("SELECT id FROM app_versions WHERE application_id = ? AND label = 'Before your edit' ORDER BY id DESC", (a["id"],))
    c.post(f"/applications/{a['id']}/restore/{version['id']}", follow_redirects=False)
    assert len(db.jload(tracker.app_with_job(a["id"])["cv_data"])["work"]) == 2, "a restored version brings its CV data back"

    base = cvdata.base()
    page = c.get("/profile?tab=cv")
    assert page.status_code == 200 and "in use" in page.text and f"/cv/base/{base['id']}/page/1.png" in page.text
    assert c.get(f"/cv/base/{base['id']}/page/1.png").status_code == 200
    assert c.get(f"/cv/base/{base['id']}/cv.pdf").content.startswith(b"%PDF")
    c.post("/cv/design", data={"primary": "#113355", "font": "Georgia", "size": "10", "sidebar": "30",
                               "main_order": "education, work", "sidebar_order": "languages skills"}, follow_redirects=False)
    cv = settings.get("cv")
    assert (cv["primary"], cv["font"], cv["size"], cv["sidebar"]) == ("#113355", "Georgia", 10.0, 30.0)
    assert cv["main_order"] == ["education", "work"] and cv["sidebar_order"] == ["languages", "skills"]
    r = c.post("/cv/base", data={"doc_id": str(base["document_id"])}, follow_redirects=False)
    assert r.status_code == 303 and db.q1("SELECT 1 FROM tasks WHERE kind = 'build_cv_base' AND status = 'queued'")
