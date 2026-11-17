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


def test_dates_and_employers_from_your_cv_are_put_back():
    base = cvdata.clean({"work": [{"name": "Havbro ApS", "position": "Intern", "start": "Jul 2024", "end": "Dec 2024"},
                                  {"name": "Nordvik P/S", "position": "Consultant", "start": "May 2026", "end": "Present"}],
                         "education": [{"institution": "VIA", "degree": "BEng", "start": "2022", "end": "2026"}]})
    tailored = cvdata.clean({"work": [{"name": "Havbro", "position": "Praktikant", "start": "aug. 2024", "end": "dec. 2024"},
                                      {"name": "Nordvik P/S", "position": "Konsulent", "start": "maj 2026", "end": "nu"}],
                             "education": [{"institution": "VIA", "degree": "BEng", "start": "2022", "end": "2025"}]})
    notes = cvdata.keep_base_facts(base, tailored)
    assert [(w["name"], w["start"], w["end"]) for w in tailored["work"]] == \
           [("Havbro ApS", "Jul 2024", "dec. 2024"), ("Nordvik P/S", "maj 2026", "nu")], "a translated date is fine, a changed one is not"
    assert tailored["education"][0]["end"] == "2026" and tailored["work"][0]["position"] == "Praktikant"
    assert len(notes) == 3 and "Jul 2024" in notes[1] and "correct it under Profile" in notes[1]


def test_a_cv_that_runs_longer_than_yours_is_cut_once(fake, monkeypatch):
    a = _draft()
    def pages(data, language=None):  # the tailored CV (support result first, two points) runs to a second page
        points = data["work"][0]["highlights"]
        return 2 if len(points) > 1 and points[0].startswith("Reducerede") else 1

    monkeypatch.setattr(writer, "_cv_pages", pages)
    job = jobs.get(a["job_id"])
    data, notes = writer.tailor_cv(job, "da", None, writer.writer_system("da", job))
    assert len(data["work"][0]["highlights"]) == 1, "the second call left a point out"
    assert any(n == "CV: Kept one point per job." for n in notes) and not any("still fills" in n for n in notes)
    assert "Shorten it to fit 1 page(s)" in fake.calls[-1][1]


def _picture(width: int = 900, height: int = 700) -> bytes:
    import io

    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (width, height), (120, 130, 140)).save(out, "JPEG")
    return out.getvalue()


def test_your_photo_is_on_the_cv_unless_the_ad_asks_for_none(fake):
    from PIL import Image

    a = _draft()
    data = db.jload(a["cv_data"])
    assert cvrender.model(data, "da")["photo"] == "" and "photo" not in data, "no photo until you add one"
    plain = cvrender.render(data, "da")

    c = TestClient(app)
    r = c.post("/cv/photo", files={"photo": ("me.txt", b"not a picture", "text/plain")}, follow_redirects=False)
    assert "picture+I+can+read" in r.headers["location"] and cvrender.photo_path() is None
    r = c.post("/cv/photo", files={"photo": ("me.jpg", _picture(), "image/jpeg")}, follow_redirects=False)
    assert "Photo+saved" in r.headers["location"]
    with Image.open(cvrender.photo_path()) as img:
        assert img.size == (cvrender.PHOTO_SIDE, cvrender.PHOTO_SIDE), "cropped to a square and made smaller"
    assert c.get("/cv/photo.jpg").headers["content-type"] == "image/jpeg" and "Replace photo" in c.get("/profile?tab=cv").text

    m = cvrender.model(data, "da")
    folder = cvrender.render(data, "da")
    assert m["photo"] == "photo.jpg" and folder != plain and (folder / "photo.jpg").exists()
    assert (folder / "cv.pdf").stat().st_size > (plain / "cv.pdf").stat().st_size, "the picture is in the PDF"

    # an ad that asks for applications without a photo: that job's CV leaves it out, and says so
    job_id = jobs.add_from_text(JOB_TEXT + "\nVi behandler ansøgninger uden billede: undlad venligst at vedlægge foto.")
    jobs.parse(job_id, use_text=True)
    app_id = jobs.create_application(job_id, draft=False)
    writer.draft(app_id)
    b = tracker.app_with_job(app_id)
    without = db.jload(b["cv_data"])
    assert without["photo"] is False and cvrender.model(without, "da")["photo"] == ""
    assert cvdata.clean(without)["photo"] is False, "saving the CV again keeps it without the photo"
    assert not (cvrender.render(without, "da") / "photo.jpg").exists()
    writer.draft(a["id"])
    assert "photo" not in db.jload(tracker.app_with_job(a["id"])["cv_data"]), "any other job's CV has the photo"

    c.post("/cv/photo/remove", follow_redirects=False)
    assert cvrender.photo_path() is None and cvrender.model(data, "da")["photo"] == ""


def test_an_application_starts_from_your_base_in_its_own_language(fake):
    a = _draft()  # the base read from the Danish CV is the one in use
    danish = cvdata.base()
    assert danish["language"] == "da" and cvdata.base_for("da")["id"] == danish["id"]
    assert cvdata.base_for("en")["id"] == danish["id"], "no English base yet: the one in use is translated"

    english = cvdata.clean({**danish["data"], "label": "Project manager and product owner",
                            "titles": {"work": "Experience", "education": "Education", "skills": "Skills"}})
    english["work"][0]["highlights"] = ["Responsible for the mobile bank's payment features with 2 million users.",
                                        "Cut support requests about payments by 30% in a year."]
    en_id = cvdata.add_base(english, "My CV in English", "en", made_by="you")
    assert cvdata.base()["id"] == danish["id"], "adding a base in another language doesn't replace the one in use"
    assert cvdata.base_for("en")["id"] == en_id and cvdata.base_for("da")["id"] == danish["id"] and cvdata.base_for("sv")["id"] == danish["id"]

    # an English application is made from the English base, word for word, not from a translation
    settings.update("writing", {"language": "en"})
    writer.draft(a["id"])
    made = db.jload(tracker.app_with_job(a["id"])["cv_data"])
    assert made["label"] == "Project manager and product owner" and "Cut support requests" in " ".join(made["work"][0]["highlights"])

    c = TestClient(app)
    page = c.get("/profile?tab=cv").text
    assert "used for en applications" in page and f"/cv/base/{danish['id']}/save" in page
    shown = c.get(f"/profile?tab=cv&base={en_id}").text
    assert f"/cv/base/{en_id}/save" in shown and f"/cv/base/{en_id}/check" in shown and "shown on this page" in shown
    r = c.post(f"/cv/base/{en_id}/save", data={"data": json.dumps(english)}, follow_redirects=False)
    assert f"base={en_id}" in r.headers["location"]
    assert f"/profile?tab=cv&amp;base={en_id}" in c.get(f"/applications/{a['id']}?tab=cv").text or \
           f"/profile?tab=cv&base={en_id}" in c.get(f"/applications/{a['id']}?tab=cv").text


def test_the_cv_fills_its_page_and_never_grows_to_do_so(fake):
    a = _draft()
    data = db.jload(a["cv_data"])
    folder = cvrender.render(data, "da")
    used = json.loads((folder / "data.json").read_text(encoding="utf-8"))["design"]["spacing"]
    assert cvrender.pages(data, "da") == 1 and used == cvrender.SPACINGS[-1], "a short CV is set as open as the layout allows"

    # add points until the page is full enough that the lines have to close up
    tight = used
    for points in range(4, 40, 2):
        long = cvdata.clean(data)
        long["work"][0]["highlights"] = [f"Punkt {n}: ansvar for betalinger i mobilbanken." for n in range(points)]
        folder = cvrender.render(long, "da")
        tight = json.loads((folder / "data.json").read_text(encoding="utf-8"))["design"]["spacing"]
        if tight < used:
            break
    assert tight < used, "a fuller CV gets tighter lines"
    m = cvrender.model(long, "da")
    least = cvrender._page_count(folder, m, cvrender.SPACINGS[0])
    assert cvrender.pages(long, "da") == least, "opening the lines never adds a page"
    nxt = cvrender.SPACINGS[cvrender.SPACINGS.index(tight) + 1]
    assert cvrender._page_count(folder, m, nxt) > least, "and it is the most open spacing that still fits"


def test_the_cv_prints_its_own_place_line_and_the_linkedin_address(fake):
    settings.update("profile", {"linkedin": "https://www.linkedin.com/in/sara-lund-123456789/"})
    a = _draft()
    data = db.jload(a["cv_data"])
    lines = [x["text"] for x in cvrender.model(data, "da")["header"]["lines"]]
    assert "København" in lines, "without a place of its own, the CV prints your city"
    assert "linkedin.com/in/sara-lund-123456789" in lines and "LinkedIn" not in lines
    link = next(x for x in cvrender.model(data, "da")["header"]["lines"] if x["text"].startswith("linkedin.com"))
    assert link["url"] == "https://www.linkedin.com/in/sara-lund-123456789/"

    data["location"] = "København, Danmark"
    lines = [x["text"] for x in cvrender.model(cvdata.clean(data), "da")["header"]["lines"]]
    assert "København, Danmark" in lines and "København" not in lines
    assert settings.get("profile")["city"] == "København", "your city, used for distances and letter dates, is untouched"
    assert cvrender.pages(cvdata.clean(data), "da") == 1
