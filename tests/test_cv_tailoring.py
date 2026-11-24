"""A job's CV reads as written for it: projects and points from the career database, and its own section order."""
from __future__ import annotations

from career_agent import cvdata, cvrender, db, tracker, writer

from .test_cv import _draft


def _answer_with(fake, monkeypatch, order: list[str], project: dict | None = None):
    """The writer's answer as the fake gives it, with a section order (and a project brought in) for this job."""
    plain = fake.answer

    def answer(system, user, schema):
        data = plain(system, user, schema)
        if schema is cvdata.TAILOR_SCHEMA:
            data["main_order"] = order
            if project:
                data["cv"]["projects"] = [project]
        return data

    monkeypatch.setattr(fake, "answer", answer)


def test_the_writer_is_asked_to_make_the_cv_read_as_written_for_the_job(fake):
    _draft()
    sent = next(user for system, user, schema in reversed(fake.calls) if schema is cvdata.TAILOR_SCHEMA)
    assert "make it read as if it was written for this job" in sent and "Change as little as you can" not in sent
    assert "from the base CV or the career database" in sent and "main_order" in sent
    assert "Never change an employer, a job title, a date or a number" in sent, "what stays, stays"


PROJECT = {"name": "Event platform", "start": "2024", "end": "2025", "description": "",
           "highlights": ["C#/.NET with DDD and CQRS."]}


def test_each_cv_gets_the_section_order_that_suits_its_job(fake, monkeypatch):
    _answer_with(fake, monkeypatch, ["projects", "work"], PROJECT)
    a = _draft()
    data = db.jload(tracker.app_with_job(a["id"])["cv_data"], {})
    assert data["main_order"] == ["projects", "work", "education"], "the rest follows in the usual order"
    heading = {k: cvrender._title(data, k, "da") for k in ("work", "projects", "education")}
    assert [s["title"] for s in cvrender.model(data, "da")["main"]] == [heading["projects"], heading["work"], heading["education"]]
    md = cvdata.to_markdown(data)
    assert md.index(f"## {heading['projects']}") < md.index(f"## {heading['work']}") < md.index(f"## {heading['education']}")

    _answer_with(fake, monkeypatch, [])  # a revision that doesn't choose keeps the order the CV had
    writer.revise(a["id"], "Put the SQL point first.", part="cv")
    assert db.jload(tracker.app_with_job(a["id"])["cv_data"], {})["main_order"][0] == "projects"

    writer.reset_cv(a["id"])  # your base CV, as it is: the usual order
    assert "main_order" not in db.jload(tracker.app_with_job(a["id"])["cv_data"], {})


def test_a_project_brought_in_from_the_database_keeps_its_own_dates(fake, monkeypatch):
    project = dict(PROJECT)
    _answer_with(fake, monkeypatch, ["work", "projects"], project)
    base = cvdata.clean({"projects": [{"name": "Webshop", "start": "2021", "end": "2022", "description": "",
                                       "highlights": ["A shop."]}]})
    tailored = cvdata.clean({"projects": [dict(project)]})
    assert cvdata.keep_base_facts(base, tailored) == [] and tailored["projects"][0]["start"] == "2024", \
        "another project in the base project's place isn't given the base project's dates"
    a = _draft()
    data = db.jload(tracker.app_with_job(a["id"])["cv_data"], {})
    assert data["projects"][0]["name"] == "Event platform" and data["projects"][0]["start"] == "2024"
