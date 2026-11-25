"""A one-page CV uses its page: one that runs over is cut where it runs over, and room left in a column is used."""
from __future__ import annotations

from career_agent import cvdata, cvrender, jobs, writer

from .test_cv import _draft
from .test_cv_lines import WORDS, _cv


def test_the_room_left_in_each_column_is_measured(fake):
    short = _cv(["Built a thing."])
    room = cvrender.room(short, "en")
    assert room["side"] > 10 and room["main"] > 10, "a short CV leaves both columns mostly empty"

    long = _cv([" ".join(WORDS * 2) + "." for _ in range(40)])
    room = cvrender.room(long, "en")
    assert cvrender.pages(long, "en") > 1 and room["main"] < -10 and room["side"] > 10, \
        "the main column runs over; the sidebar doesn't"


def test_what_the_sidebar_left_out_is_listed():
    base = cvdata.clean({"skills": [{"name": "Tools", "keywords": ["Python", "SQL", "Docker"]}], "interests": ["Chess", "Running"],
                         "languages": [{"language": "Danish"}, {"language": "English"}]})
    cv = cvdata.clean({"skills": [{"name": "Backend", "keywords": ["python", "SQL"]}], "interests": ["Running"],
                       "languages": [{"language": "Danish"}, {"language": "English"}]})
    assert cvdata.left_out_of_sidebar(base, cv) == ["Skills: Docker", "Interests: Chess"]


def _pages_and_room(monkeypatch, over: dict, fits: dict, too_long=lambda data: False):
    """The tailored CV (support result first, two points) runs over the page by `over`; cut to one point it fits,
    with `fits` left. `too_long` says which other CVs run over."""
    def runs_over(data):
        points = data["work"][0]["highlights"]
        return (len(points) > 1 and points[0].startswith("Reducerede")) or too_long(data)

    monkeypatch.setattr(writer, "_cv_pages", lambda data, language=None: 2 if runs_over(data) else 1)
    monkeypatch.setattr(writer, "_cv_room", lambda data, language, spacing=None: over if runs_over(data) else fits)


def _tailor(fake, a):
    """Tailor the CV for this job again; what the writer was asked from here on is in fake.asked."""
    job = jobs.get(a["job_id"])
    fake.since = len(fake.calls)
    return writer.tailor_cv(job, "da", None, writer.writer_system("da", job))


def _asked(fake, words: str) -> list[str]:
    return [user for system, user, schema in fake.calls[fake.since:] if schema is cvdata.TAILOR_SCHEMA and words in user]


def test_a_cv_that_runs_over_is_cut_in_the_column_that_runs_over(fake, monkeypatch):
    a = _draft()
    _pages_and_room(monkeypatch, over={"side": 6, "main": -4}, fits={"side": 0.5, "main": 0.2})
    data, notes = _tailor(fake, a)
    asked = _asked(fake, "fills 2 pages")
    assert len(asked) == 1 and "The main column runs over the page by about 4 lines" in asked[0]
    assert "The sidebar is not what runs over, so leave it as it is" in asked[0], "cutting the sidebar wouldn't help"
    assert len(data["work"][0]["highlights"]) == 1 and not any("still fills" in n for n in notes)
    assert not _asked(fake, "room to spare"), "a column with half a line left has nothing to fill"


def test_room_left_in_the_sidebar_is_used_and_kept_only_if_it_fits(fake, monkeypatch):
    a = _draft()
    _pages_and_room(monkeypatch, over={"side": 6, "main": -4}, fits={"side": 6, "main": 0.2},
                    too_long=lambda data: "Skak" in data["interests"])
    plain = fake.answer

    def answer(system, user, schema):  # asked to fill the room, it brings in an interest
        data = plain(system, user, schema)
        if schema is cvdata.TAILOR_SCHEMA and "room to spare" in user:
            data["cv"]["interests"] = [*data["cv"]["interests"], "Skak"]
            data["changes"] = ["Added chess under interests."]
        return data

    monkeypatch.setattr(fake, "answer", answer)
    data, notes = _tailor(fake, a)
    asked = _asked(fake, "room to spare")
    assert len(asked) == 1 and "the sidebar has room for about 6 lines more" in asked[0]
    assert "main column has room" not in asked[0], "only where there is room"
    assert any("What you just added to use the spare room was more than the room" in u for u in _asked(fake, "fills 2 pages")), \
        "told it went over, by how much"
    assert "Skak" not in data["interests"] and "CV: Added chess under interests." not in notes, \
        "it still ran over when the passes ran out, so the last CV that fitted stays"
    assert len(data["work"][0]["highlights"]) == 1 and not any("still fills" in n for n in notes)

    monkeypatch.setattr(writer, "_cv_pages", lambda data, language=None:
                        2 if len(data["work"][0]["highlights"]) > 1 and data["work"][0]["highlights"][0].startswith("Reducerede") else 1)
    data, notes = _tailor(fake, a)
    assert "Skak" in data["interests"] and "CV: Added chess under interests." in notes, "when it fits, it's kept"


def test_a_cv_that_still_runs_over_is_cut_again(fake, monkeypatch):
    a = _draft()
    base = cvdata.base_for("da")["data"]
    monkeypatch.setattr(writer, "_cv_pages", lambda data, language=None: 1 if data == base else 2)  # however much is cut
    monkeypatch.setattr(writer, "_cv_room", lambda data, language, spacing=None: {"side": 1, "main": -3})
    data, notes = _tailor(fake, a)
    assert len(_asked(fake, "fills 2 pages")) == writer.PAGE_TRIES
    assert any("still fills more than 1 page" in n for n in notes)


def test_room_is_measured_at_your_base_cvs_line_spacing(fake, monkeypatch):
    a = _draft()
    asked_at = []
    monkeypatch.setattr(writer, "_cv_room", lambda data, language, spacing=None: asked_at.append(spacing) or {"side": 0, "main": 0})
    _tailor(fake, a)
    base = cvdata.base_for("da")["data"]
    assert asked_at and set(asked_at) == {cvrender.spacing(base, "da")}, \
        "room a CV has only because its lines are set further apart than yours is room for content"


def test_dates_are_printed_the_same_way_everywhere():
    assert cvdata.period({"start": "2023-09", "end": "2023-12"}, "da") == "sep. 2023 – dec. 2023"
    assert cvdata.period({"start": "2023-09", "end": "Present"}, "en") == "Sep 2023 – Present"
    assert cvdata.period({"start": "2025", "end": "2025"}, "da") == "2025", "not 2025 – 2025"
    assert cvdata.period({"start": "aug. 2024", "end": "dec. 2024"}, "da") == "aug. 2024 – dec. 2024", "as written"
    cv = cvdata.clean({"language": "da", "projects": [{"name": "Webshop", "start": "2023-09", "end": "2023-12", "highlights": ["A shop."]}]})
    assert cvrender.model(cv, "da")["main"][0]["entries"][0]["b"] == "sep. 2023 – dec. 2023"
    assert "sep. 2023 – dec. 2023" in cvdata.to_markdown(cv)


def test_texts_that_run_long_are_shortened_unless_they_are_your_own(fake):
    yours = " ".join(WORDS * 2) + "."  # three lines, in your base CV: yours to keep
    new = " ".join(reversed(WORDS * 2)) + "."  # three lines, written for this job
    base = _cv([yours, "Short."])
    data = _cv([yours, new])
    data["label"] = "Software Engineer · Backend, data pipelines, integrations and AI agents"
    fake.since = len(fake.calls)
    notes = writer.tidy_lines({"title": "Developer", "company_name": "Acme"}, "en", "system", data, base)
    sent = next(user for system, user, schema in fake.calls[fake.since:] if schema is writer.SPILL_SCHEMA)
    point, headline = sent.split("takes more than 2 lines", 1)[1], sent.split("doesn't fit on its line", 1)[1]
    assert new in point and yours not in sent, "your own wording isn't touched"
    assert "Software Engineer · Backend, data pipelines" in headline
    assert data["work"][0]["highlights"][0] == yours and data["work"][0]["highlights"][1] != new
    assert data["label"] != "Software Engineer · Backend, data pipelines, integrations and AI agents"
    assert any("made a little shorter" in n for n in notes)


def test_a_column_that_squeezes_the_page_is_shortened(fake, monkeypatch):
    a = _draft()
    plain = fake.answer

    def answer(system, user, schema):  # asked to shorten the sidebar, it leaves out two skills
        data = plain(system, user, schema)
        if schema is cvdata.TAILOR_SCHEMA and "longer than the page holds at the usual spacing" in user:
            data["cv"]["skills"][0]["keywords"] = data["cv"]["skills"][0]["keywords"][:3]
        return data

    monkeypatch.setattr(fake, "answer", answer)
    crowded = lambda data: len(data["skills"][0]["keywords"]) > 3
    monkeypatch.setattr(writer, "_cv_pages", lambda data, language=None: 1)  # it fits: at a tighter spacing
    monkeypatch.setattr(writer, "_cv_room", lambda data, language, spacing=None:
                        {"side": -0.9 if crowded(data) else 0.5, "main": 0.2})
    data, notes = _tailor(fake, a)
    asked = _asked(fake, "longer than the page holds at the usual spacing")
    assert len(asked) == 1 and "the sidebar is about 1 line longer" in asked[0]
    assert "Leave the main column as it is" in asked[0], "the column with the gap is not the one to cut"
    assert len(data["skills"][0]["keywords"]) == 3 and not any("still fills" in n for n in notes)
