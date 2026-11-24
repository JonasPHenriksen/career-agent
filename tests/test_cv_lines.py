"""No line of a one-page CV holds just a word: set a touch tighter where that saves the line, else shortened."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from career_agent import cvdata, cvrender, db, tracker, writer
from career_agent.web.app import app

from .test_cv import _draft

WORDS = "Built reliable integrations between warehouse systems, carriers and the people who use them".split()


def _cv(bullets: list[str]) -> dict:
    return cvdata.clean({"language": "en", "label": "Developer", "summary": "Builds things.",
                         "work": [{"name": "Acme", "position": "Developer", "start": "2022", "end": "Present",
                                   "highlights": bullets}]})


def test_short_last_lines_are_found_and_tightened_where_that_saves_a_line(fake):
    bullets = [" ".join((WORDS * 6)[:n]) + "." for n in range(8, 44)]  # every length, so every kind of last line
    data = _cv(bullets)
    folder = cvrender.render(data, "en")
    tight = json.loads((folder / "data.json").read_text(encoding="utf-8"))["tight"]
    short = cvrender.spills(data, "en")
    assert tight and short, "some lines fit when set tighter, some don't"
    assert all(x["kind"] == "bullet" and x["text"] in bullets for x in short)
    assert not set(tight) & {x["text"] for x in short}, "a line tightening fixes isn't reported"

    plain = _cv(bullets[:1])  # a one-line point: nothing to tighten, nothing to report
    assert json.loads((cvrender.render(plain, "en") / "data.json").read_text(encoding="utf-8"))["tight"] == []
    assert cvrender.spills(plain, "en") == []


def test_a_line_is_found_where_the_cv_prints_it():
    data = _cv(["First point.", "Second point."])
    data["skills"] = [{"name": "Tools", "keywords": ["Python", "SQL", "Docker"]}]
    assert cvdata.replace_text(data, "Second point. ", "Second.") and data["work"][0]["highlights"] == ["First point.", "Second."]
    assert cvdata.replace_text(data, "Python, SQL, Docker", "Python, SQL") and data["skills"][0]["keywords"] == ["Python", "SQL"]
    assert cvdata.replace_text(data, "Builds things.", "Builds.") and data["summary"] == "Builds."
    assert not cvdata.replace_text(data, "Never printed", "x")


def test_tailoring_shortens_a_line_that_ends_with_a_word_alone(fake, monkeypatch):
    seen = []

    def spills(data, language=None):  # the first look finds the CV's first point spilling; after that it fits
        seen.append(data["work"][0]["highlights"][0])
        return [{"kind": "bullet", "text": seen[0]}] if len(seen) == 1 else []

    monkeypatch.setattr(cvrender, "spills", spills)
    a = _draft()
    data = db.jload(tracker.app_with_job(a["id"])["cv_data"], {})
    assert data["work"][0]["highlights"][0] == seen[0].rsplit(" ", 1)[0], "the point lost its last word"
    sent = next(user for system, user, schema in reversed(fake.calls) if schema is writer.SPILL_SCHEMA)
    assert f"[1] {seen[0]}" in sent and sent.count("\n[") == 1, "only the line that spills goes to the model"
    assert any("made a little shorter" in n for n in db.jload(tracker.app_with_job(a["id"])["writer_notes"], []))


def test_the_base_cv_page_lists_its_own_spilling_lines(fake, monkeypatch):
    a = _draft()
    base = cvdata.base()
    first = base["data"]["work"][0]["highlights"][0]
    monkeypatch.setattr(cvrender, "spills", lambda data, language=None: [{"kind": "bullet", "text": first}])
    page = TestClient(app).get("/profile?tab=cv").text
    assert 'id="cv-spills"' in page and first in page and "shorten it here" in page
    assert a["id"]
