"""What changed between two drafts: the letter word by word, the CV line by line, and the review screen showing it."""
from __future__ import annotations

import re

from fastapi.testclient import TestClient

from career_agent import compare, cvdata, db, tracker, writer
from career_agent.web.app import app

from .test_web import _seed

OLD = """Kære Mette

Jeg søger stillingen som product owner hos FinPay. I Danske Bank har jeg ansvaret for betalingerne i mobilbanken.

Jeg kan lide at være tæt på brugerne. Jeg har ledet et team på 8 udviklere.

Med venlig hilsen
Sara Lund"""


def _marked(html: str, tag: str) -> list[str]:
    return [re.sub(r"<[^>]+>", "", m) for m in re.findall(rf'<{tag} class="chg">(.*?)</{tag}>', str(html))]


def test_a_reworded_sentence_marks_only_the_words_that_changed():
    new = OLD.replace("ansvaret for betalingerne", "ansvaret for betalingsfunktionerne")
    d = compare.letter(OLD, new)
    assert d["counts"] == {"reworded": 1, "added": 0, "removed": 0}
    assert _marked(d["html"], "del") == ["betalingerne"] and _marked(d["html"], "ins") == ["betalingsfunktionerne"]
    assert "Jeg kan lide at være tæt på brugerne." in str(d["html"]), "what didn't change is shown as it is"
    assert compare.summary_letter(d).startswith("1 sentence reworded")


def test_added_and_removed_sentences_and_paragraphs():
    new = OLD.replace(" Jeg har ledet et team på 8 udviklere.", "") + ""
    new = new.replace("Med venlig hilsen", "Jeg starter gerne 1. marts.\n\nMed venlig hilsen")
    d = compare.letter(OLD, new)
    assert d["counts"]["removed"] == 1 and d["counts"]["added"] == 1
    assert _marked(d["html"], "del") == ["Jeg har ledet et team på 8 udviklere."]
    assert "Jeg starter gerne 1. marts." in "".join(_marked(d["html"], "ins"))
    assert str(d["html"]).count("<p>") == 5, "paragraphs stay paragraphs"
    assert not d["rewritten"]


def test_spacing_and_browser_line_breaks_are_not_changes():
    d = compare.letter(OLD, OLD.replace("\n", "\r\n").replace("FinPay. I", "FinPay.  I"))
    assert d["same"] and "chg" not in str(d["html"])


def test_a_letter_written_again_is_told_apart_from_a_revision():
    new = "Hej Mette\n\nHelt ny tekst om noget andet. Den handler om data. Og om mennesker.\n\nVenlig hilsen\nSara"
    d = compare.letter(OLD, new)
    assert d["rewritten"] and "mostly written again" in compare.summary_letter(d)


def _cv(**changes) -> dict:
    data = {"language": "da", "label": "Product Owner", "summary": "Erfaren product owner med betalinger.",
            "work": [{"name": "Danske Bank", "position": "Product Owner", "start": "2022", "end": "nu",
                      "highlights": ["Ansvarlig for betalingsfunktionerne", "Reducerede henvendelser med 30 %",
                                     "Ledte et team på 8 udviklere"]}],
            "education": [{"institution": "CBS", "degree": "Cand.merc.it", "area": "", "start": "2016", "end": "2018",
                           "highlights": []}],
            "skills": [{"name": "Metoder", "keywords": ["Scrum", "Jira", "SQL"]}]}
    data.update(changes)
    return cvdata.clean(data)


def test_cv_lines_reworded_added_removed_and_moved():
    old = _cv()
    work = [{**old["work"][0], "highlights": ["Reducerede henvendelser med 30 %", "Ansvarlig for alle betalingsfunktionerne",
                                              "Arbejdede tæt med jura"]}]
    new = _cv(work=work, skills=[{"name": "Metoder", "keywords": ["SQL", "Scrum", "Jira"]}], color="#0a7a5a")
    d = compare.cv(cvdata.to_markdown(old), cvdata.to_markdown(new), old, new)
    kinds = {it["kind"]: it for g in d["groups"] for it in g["lines"]}
    assert d["counts"] == {"changed": 1, "reordered": 1, "added": 1, "removed": 1, "moved": 0}
    assert _marked(kinds["changed"]["html"], "ins") == ["alle"], "only the new word in a reworded point is marked"
    assert kinds["changed"]["moved"], "a point reworded in another place says it moved too"
    assert kinds["removed"]["line"] == "- Ledte et team på 8 udviklere"
    assert kinds["added"]["line"] == "- Arbejdede tæt med jura"
    assert kinds["reordered"]["line"].startswith("- Metoder: SQL"), "the same skills in a new order aren't a rewrite"
    group = next(g for g in d["groups"] if kinds["changed"] in g["lines"])
    assert group["entry"] == "Product Owner — Danske Bank", "each change says which job it's under"
    assert d["look"] == [{"what": "colour", "old": None, "new": "#0a7a5a", "by": "company"}]
    line = compare.summary_cv(d)
    assert "1 line reworded" in line and "1 line taken out" in line and "new colour" in line


def test_a_cv_point_that_only_moved():
    points = ["Ansvarlig for betalingsfunktionerne", "Reducerede henvendelser med 30 %", "Ledte et team på 8 udviklere",
              "Arbejdede tæt med jura"]
    old = _cv(work=[{**_cv()["work"][0], "highlights": points}])
    new = _cv(work=[{**_cv()["work"][0], "highlights": points[1:] + points[:1]}])
    d = compare.cv(cvdata.to_markdown(old), cvdata.to_markdown(new), old, new)
    assert d["counts"] == {"changed": 0, "reordered": 0, "added": 0, "removed": 0, "moved": 1}
    assert compare.summary_cv(d) == "1 line moved"


def test_cv_sections_in_a_new_order():
    old, new = _cv(), _cv(main_order=["education", "work", "projects"])
    d = compare.cv(cvdata.to_markdown(old), cvdata.to_markdown(new), old, new)
    assert d["order"] and d["order"][1][0] == "Uddannelse" and not d["counts"]["changed"]
    assert not d["same"] and "sections in a new order" in compare.summary_cv(d)


def _version(app_id: int, label: str, asked: str | None = None, **text) -> int:
    a = db.q1("SELECT * FROM applications WHERE id = ?", (app_id,))
    row = {k: text.get(k, a[k]) for k in ("subject", "letter", "cv", "cv_data")}
    return db.insert("app_versions", {"application_id": app_id, "label": label, "asked": asked, "created_at": db.now(), **row})


def test_the_review_screen_shows_what_a_revision_changed(fake):
    _, _, app_id = _seed()
    before = tracker.app_with_job(app_id)["letter"]
    writer.revise(app_id, "Skriv at jeg kan starte 1. marts.")  # the fake writer gives the same letter back
    versions = db.q("SELECT * FROM app_versions WHERE application_id = ? ORDER BY id DESC", (app_id,))
    assert versions[0]["label"] == "Before revision" and versions[0]["asked"] == "Skriv at jeg kan starte 1. marts."
    chg = compare.for_app(tracker.app_with_job(app_id), versions)
    assert chg["idle"] and chg["idle"]["asked"].startswith("Skriv"), "a revision that changed nothing says so"

    new_letter = before.replace("Med venlig hilsen", "Jeg kan starte 1. marts.\n\nMed venlig hilsen")
    _version(app_id, "Before revision", asked="Skriv at jeg kan starte 1. marts.")
    db.update("applications", app_id, {"letter": new_letter})
    c = TestClient(app)
    page = c.get(f"/applications/{app_id}?tab=review").text
    assert "What changed" in page and "the draft before your revision" in page
    assert '<ins class="chg">Jeg kan starte 1. marts.</ins>' in page, "the review screen opens on the changes"
    assert "Skriv at jeg kan starte 1. marts." in page, "and shows what was asked for"
    assert "The CV is unchanged since this version." in page
    clean = c.get(f"/applications/{app_id}?tab=review&view=clean").text
    assert '<ins class="chg">' not in clean and "Show the changes" in clean
    assert "See what changed" in c.get(f"/applications/{app_id}?tab=letter").text
    history = c.get(f"/applications/{app_id}?tab=history").text
    assert "Compare with now" in history and "Then asked for" in history


def test_comparing_with_an_older_version_and_the_cv_tab(fake):
    _, _, app_id = _seed()
    a = tracker.app_with_job(app_id)
    data = db.jload(a["cv_data"], None)
    first = _version(app_id, "Before your edit")
    if data:  # the CV as data: one point taken out
        data["work"][0]["highlights"] = data["work"][0]["highlights"][1:]
        db.update("applications", app_id, {"cv_data": db.jdump(data), "cv": cvdata.to_markdown(data)})
    else:
        db.update("applications", app_id, {"cv": a["cv"] + "\n- Et nyt punkt\n"})
    _version(app_id, "Before your edit")  # the same as now: skipped when choosing what to compare with
    c = TestClient(app)
    page = c.get(f"/applications/{app_id}?tab=cv").text
    assert "What changed in the CV since the draft before your edit" in page and 'class="cv-diff"' in page
    page = c.get(f"/applications/{app_id}?tab=review&against={first}").text
    assert f'<option value="{first}" selected>' in page and "same as now" in page
