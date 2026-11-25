"""A job's CV is your base CV, tailored lightly: its top and its order change for the job, the rest is yours."""
from __future__ import annotations

from career_agent import cvdata, cvrender, db, jobs, tracker, writer

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


def test_the_writer_is_asked_to_tailor_lightly(fake):
    _draft()
    sent = next(user for system, user, schema in reversed(fake.calls) if schema is cvdata.TAILOR_SCHEMA)
    assert "Tailor only its top and its order" in sent and "make it read as if it was written for this job" not in sent
    assert "the CV's projects as they are" in sent and "swap at most one point" in sent, "your projects and points stay"
    assert "no list of personal qualities" in sent and f"at most {cvdata.HEADLINE_CHARS} characters" in sent
    assert "Never change an employer, a job title, a date or a number" in sent, "what stays, stays"


PROJECT = {"name": "Event platform", "start": "2024", "end": "2025", "description": "",
           "highlights": ["C#/.NET with DDD and CQRS."]}


def test_a_cv_keeps_your_section_order_unless_you_ask_for_another(fake, monkeypatch):
    _answer_with(fake, monkeypatch, ["projects", "work"], PROJECT)
    a = _draft()
    data = db.jload(tracker.app_with_job(a["id"])["cv_data"], {})
    assert "main_order" not in data, "the writer's own idea of an order doesn't change your CV's"

    writer.revise(a["id"], "Put the projects first.", part="cv")  # asked for: then it does
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


def test_the_ads_words_your_career_database_backs_are_given_to_the_writer(fake):
    a = _draft()
    job = jobs.get(a["job_id"])
    base = cvdata.base()["data"]
    words = writer.ad_words(job, base)
    assert "Scrum" not in words and "SQL" not in words, "already on the CV"
    assert "Kubernetes" not in words, "nothing in the career database backs it: never offered"

    db.insert("facts", {"category": "skill", "title": "Kubernetes", "detail": "Ran the payment services on Kubernetes.",
                        "verified": 1, "hidden": 0, "source": "you", "created_at": db.now(), "updated_at": db.now()})
    assert "Kubernetes" in writer.ad_words(job, base)
    writer.revise(a["id"], "Make it fit the ad.", part="cv")
    sent = next(user for system, user, schema in reversed(fake.calls) if schema is cvdata.TAILOR_SCHEMA and "<cv>" in user)
    line = sent.split("THE AD'S OWN WORDS THAT THE CV DOESN'T USE YET: ", 1)[1].split("\n", 1)
    assert "Kubernetes" in line[0] and "never add a skill the candidate doesn't have" in line[1]


def test_a_skill_you_only_have_insight_in_is_never_offered_or_added(fake):
    a = _draft()
    job = jobs.get(a["job_id"])
    base = cvdata.base()["data"]
    fid = db.insert("facts", {"category": "skill", "title": "Kubernetes", "depth": "insight", "verified": 1,
                              "detail": "The team's services ran on Kubernetes.", "hidden": 0, "source": "you",
                              "created_at": db.now(), "updated_at": db.now()})
    assert "Kubernetes" not in writer.ad_words(job, base), "you know of it, but haven't used it"
    db.update("facts", fid, {"depth": "coursework"})
    assert "Kubernetes" in writer.ad_words(job, base), "used in a course counts as used"
    assert "never one marked [insight only]" in cvdata.TAILOR_RULES
    assert "Never one marked [insight only]" in writer.CV_RULES
    assert "[insight only]" in writer.CHECK_SYSTEM, "the fact check marks it as partly backed"
