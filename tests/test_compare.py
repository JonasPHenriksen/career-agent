"""What a revision changed: the letter word by word, the CV line by line, and the pop-up to keep, revert or change it."""
from __future__ import annotations

import re

from fastapi.testclient import TestClient

from career_agent import compare, cvdata, db, review, tracker, writer
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


def _revise_by_hand(app_id: int, letter: str | None = None, point: tuple[str, str] | None = None,
                    asked: str = "Gør den lidt kortere.") -> None:
    """A revision as writer.revise leaves it, without a model: the draft before it under Versions, the new draft,
    and the record the pop-up reads. `point` rewrites a point of the CV: (as it was, as it becomes)."""
    a = db.q1("SELECT * FROM applications WHERE id = ?", (app_id,))
    before = {k: a[k] for k in compare.DERIVED}
    version_id = writer._save_version(app_id, "Before revision", asked)
    patch: dict = {}
    if letter:
        patch["letter"] = letter
    if point:
        data = db.jload(a["cv_data"], None)
        assert review.replace_in_cv(data, *point)
        patch.update({"cv_data": db.jdump(data), "cv": cvdata.to_markdown(data)})
    after = {k: patch.get(k, a[k]) for k in compare.TEXT}
    db.update("applications", app_id, {**patch, "revision": db.jdump(compare.record(version_id, "both", before, after))})


def test_a_revision_opens_a_pop_up_to_keep_it(fake):
    _, _, app_id = _seed()
    a = tracker.app_with_job(app_id)
    db.update("applications", app_id, {"letter": a["letter"].replace("på 12 udviklere", "på mange udviklere")})
    writer.revise(app_id, "Nævn holdets størrelse.")  # the fake writer gives its own letter back: "12" again
    rev = compare.revision(tracker.app_with_job(app_id))
    assert rev["open"] and rev["letter_changed"] and not rev["cv_changed"] and rev["asked"] == "Nævn holdets størrelse."
    assert _marked(rev["letter"]["html"], "del") == ["mange"] and _marked(rev["letter"]["html"], "ins") == ["12"]

    c = TestClient(app)
    page = c.get(f"/applications/{app_id}?tab=review").text
    assert '<dialog id="revision"' in page and f'data-auto-open="revision-{app_id}-{rev["id"]}"' in page, "it opens by itself"
    assert "Nævn holdets størrelse." in page and '<del class="chg">mange</del>' in page
    assert 'data-dialog-open="#revision"' in page and "See the last revision" in page
    r = c.post(f"/applications/{app_id}/revision/keep", data={"tab": "letter"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith(f"/applications/{app_id}?tab=letter&msg=")
    page = c.get(f"/applications/{app_id}?tab=letter").text
    assert "data-auto-open" not in page and "You kept it" in page, "kept: the button still opens it, it doesn't open by itself"
    r = c.post(f"/applications/{app_id}/revision/keep", follow_redirects=False)
    assert "err=" in r.headers["location"], "decided once"


def test_reverting_puts_back_the_draft_and_its_fact_check(fake):
    _, _, app_id = _seed()
    a = tracker.app_with_job(app_id)
    db.update("applications", app_id, {"letter": a["letter"].replace("på 12 udviklere", "på mange udviklere")})
    writer.run_checks(app_id)
    before = db.q1("SELECT subject, letter, cv, cv_data, claims, voice, review, writer_notes FROM applications WHERE id = ?", (app_id,))
    writer.revise(app_id, "Nævn holdets størrelse.")
    revised = db.q1("SELECT letter FROM applications WHERE id = ?", (app_id,))["letter"]
    assert revised != before["letter"]

    c = TestClient(app)
    r = c.post(f"/applications/{app_id}/revision/revert", data={"tab": "review"}, follow_redirects=False)
    assert "err=" not in r.headers["location"]
    assert db.q1("SELECT subject, letter, cv, cv_data, claims, voice, review, writer_notes FROM applications WHERE id = ?",
                 (app_id,)) == before, "the draft and its fact check are back as they were, with no new check"
    kept = db.q1("SELECT letter FROM app_versions WHERE application_id = ? AND label = 'Before reverting a revision'", (app_id,))
    assert kept and kept["letter"] == revised, "the revised draft stays under Versions"
    rev = compare.revision(tracker.app_with_job(app_id))
    assert rev["status"] == "reverted" and not rev["open"]
    assert "You went back to the draft before it" in c.get(f"/applications/{app_id}").text


def test_changing_a_revision_a_little(fake):
    _, _, app_id = _seed()
    a = tracker.app_with_job(app_id)
    point = db.jload(a["cv_data"], None)["work"][0]["highlights"][0]
    _revise_by_hand(app_id, letter=a["letter"] + "\n\nJeg kan starte 1. marts.", point=(point, point + " i to år"))
    rev = compare.revision(tracker.app_with_job(app_id))
    assert rev["open"] and rev["letter_changed"] and rev["cv_changed"]
    assert [e["text"] for e in rev["edit_lines"]] == [point + " i to år"], "the reworded CV point can be changed here"
    assert rev["edit_lines"][0]["where"] == "Erfaring › Product Owner — Danske Bank"

    c = TestClient(app)
    page = c.get(f"/applications/{app_id}?tab=review").text
    assert "Change it a little" in page and 'name="old_1"' in page
    r = c.post(f"/applications/{app_id}/revision/adjust", follow_redirects=False,
               data={"tab": "review", "subject": a["subject"], "letter": a["letter"] + "\n\nJeg kan starte 1. april.",
                     "old_1": point + " i to år", "new_1": point + " i tre år"})
    assert "err=" not in r.headers["location"], r.headers["location"]
    now = tracker.app_with_job(app_id)
    assert now["letter"].endswith("Jeg kan starte 1. april.")
    assert db.jload(now["cv_data"], None)["work"][0]["highlights"][0] == point + " i tre år" and "i tre år" in now["cv"]
    assert compare.revision(now)["status"] == "edited"
    assert db.q1("SELECT 1 FROM app_versions WHERE application_id = ? AND label = 'Before your edit'", (app_id,))


def test_a_draft_changed_after_the_revision_cannot_be_kept_or_reverted_from_the_pop_up(fake):
    _, _, app_id = _seed()
    a = tracker.app_with_job(app_id)
    _revise_by_hand(app_id, letter=a["letter"] + "\n\nJeg kan starte 1. marts.")
    db.update("applications", app_id, {"letter": "Kære Mette\n\nSkrevet om af mig selv."})  # Save edits, say
    rev = compare.revision(tracker.app_with_job(app_id))
    assert not rev["current"] and not rev["open"] and rev["letter_changed"], "it still shows what the revision changed"
    c = TestClient(app)
    page = c.get(f"/applications/{app_id}?tab=review").text
    assert "data-auto-open" not in page and "The draft has been changed after this revision" in page
    r = c.post(f"/applications/{app_id}/revision/revert", follow_redirects=False)
    assert "err=" in r.headers["location"] and db.q1("SELECT letter FROM applications WHERE id = ?", (app_id,))["letter"].endswith("selv.")
