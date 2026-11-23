"""Copies the rules can't be sure of: close ads are shown to the model, and a job is set aside only when it's certain."""
from __future__ import annotations

import re

import pytest

from career_agent import db, jobs, llm, settings, sources


def _text(topic: str, n: int = 300) -> str:
    return " ".join(f"{topic}{i % 89} sentence{i} about the {topic} work and the team we have" for i in range(n))


@pytest.fixture()
def ads(fake):
    """The fake model reads TITLE=, COMPANY= and LOC= from the ad; same_job answers what the test sets."""
    settings.update("profile", {"city": "Aarhus"})
    base = fake.answer
    fake.same = {"same": 0, "reason": "Not certain."}

    def answer(system, user, schema):
        if schema is jobs.SAME_JOB_SCHEMA:
            if isinstance(fake.same, Exception):
                raise fake.same
            return fake.same
        data = base(system, user, schema)
        if schema is jobs.PARSE_SCHEMA:
            for key, field in (("TITLE", "title"), ("COMPANY", "company"), ("LOC", "location")):
                m = re.search(rf"{key}=([^\n.]*)\.", user)
                data[field] = m.group(1).strip() if m else ""
            data["deadline"] = ""
        return data
    fake.answer = answer
    return fake


def _add(n: int, title: str, company: str, loc: str, body: str, source: str = "jobindex") -> int:
    sid = (db.q1("SELECT id FROM sources WHERE name = ?", (source,)) or {}).get("id") or sources.add(source, f"https://{source}.example/feed")
    job_id, _ = jobs.add_from_url(f"https://{source}.example/job/{n}", source_id=sid, title=title,
                                  text=f"TITLE={title}. COMPANY={company}. LOC={loc}. " + body)
    jobs.parse(job_id, use_text=True)
    return job_id


def _asked(fake) -> list[str]:
    return [user for _, user, schema in fake.calls if schema is jobs.SAME_JOB_SCHEMA]


def test_danish_letters_and_nearby_towns():
    assert jobs.same_company("Scanvægt Systems A/S", "Scanvaegt Systems A/S")
    assert jobs.same_key("Scanvægt", "Embedded Software Developer") == jobs.same_key("Scanvaegt", "Embedded Software Developer")
    assert jobs.same_title("Programmør", "Programmoer") and jobs.same_place("Århus", "Aarhus")
    assert jobs.same_place("8220 Brabrand", "Aarhus, Denmark") and jobs.same_place("Nordhavn, Åbyhøj", "Aarhus, København")
    assert not jobs.same_place("Hørning", "Aarhus"), "13 km apart: two places"
    assert not jobs.same_place("Aarhus", "København") and not jobs.same_place("Athens, Greece", "Aarhus")


def test_brackets_that_say_what_the_job_is_keep_two_jobs_apart():
    assert not jobs.same_name("Teton", "Software Engineer (Backend)", "Teton.ai ApS", "Software Engineer(Fleet)")
    assert not jobs.same_name("Netcompany", "Associate Consultant (Swedish speaking)", "Netcompany", "Associate Consultant")
    assert jobs.same_name("FinPay", "Product Owner (m/k)", "FinPay A/S", "Product Owner [Hybrid]")
    assert jobs.same_name("FinPay", "Data Analyst (m/w/d)", "FinPay", "Data Analyst (Aarhus)")
    assert jobs.same_name("BDO", "Konsulent til Risk Assurance – København eller Aarhus", "BDO", "Konsulent til Risk Assurance")
    assert jobs.same_name("Lunar", "Backend-udvikler, Århus", "Lunar", "Backend-udvikler")


def test_a_reworded_copy_is_set_aside_when_the_model_is_certain(ads):
    first = _add(1, "IT-supportkonsulent", "ECIT Solutions", "Aarhus", _text("support"))
    ads.same = {"same": 1, "reason": "Same team, same tasks, the second title only adds the department."}
    second = _add(2, "IT-supportkonsulent til Managed Service Provider", "ECIT Solutions A/S", "8220 Brabrand",
                  _text("helpdesk"), source="jobnet")
    job = jobs.get(second)
    assert job["status"] == "filtered" and job["duplicate_of"] == first
    assert "judged them the same opening (Same team, same tasks" in job["notes"] and "Keep it as its own job" in job["notes"]
    asked = _asked(ads)
    assert len(asked) == 1 and "NEW AD: IT-supportkonsulent til Managed" in asked[0] and "\n1: IT-supportkonsulent\n" in asked[0]
    assert not db.q1("SELECT 1 FROM tasks WHERE kind = 'score_job' AND ref = ?", (f"job:{second}",)), "a copy isn't scored"


def test_the_job_stays_when_the_model_is_not_certain_or_cannot_be_reached(ads):
    _add(1, "IT-supportkonsulent", "ECIT Solutions", "Aarhus", _text("support"))
    kept = _add(2, "IT-supportkonsulent til Managed Service Provider", "ECIT Solutions", "Aarhus", _text("helpdesk"), "jobnet")
    ads.same = llm.LLMError("The model is busy.")
    also_kept = _add(3, "Senior IT-supportkonsulent", "ECIT Solutions", "Aarhus", _text("senior"), "it-jobbank")
    ads.same = {"same": 7, "reason": "A number that isn't on the list."}
    still_kept = _add(4, "IT-supportkonsulent, 2nd level", "ECIT Solutions", "Aarhus", _text("level"), "jobbank")
    for job_id in (kept, also_kept, still_kept):
        assert jobs.get(job_id)["status"] == "new" and jobs.get(job_id)["duplicate_of"] is None
    assert len(_asked(ads)) == 3


def test_the_same_text_with_the_company_missing_is_shown_to_the_model(ads):
    body = _text("defence")
    first = _add(1, "Senior developer til Forsvarets AI Center", "Forsvaret", "Aarhus", body)
    ads.same = {"same": 1, "reason": "The same ad; one site leaves out the employer."}
    second = _add(2, "Senior developer til Forsvarets AI Center, Personelkommandoen", "", "Aarhus",
                  body + " Personelkommandoen", source="jobnet")
    assert jobs.get(second)["duplicate_of"] == first


def test_no_model_call_when_nothing_is_close(ads):
    _add(1, "IT-supportkonsulent", "ECIT Solutions", "Aarhus", _text("support"))
    _add(2, "Data Engineer", "JYSK", "Aarhus", _text("data"), "jobnet")  # another company, another text
    _add(3, "IT-supportkonsulent", "ECIT Solutions", "København", _text("capital"), "it-jobbank")  # another town
    assert _asked(ads) == []


def test_a_filtered_or_set_aside_ad_never_stands_in_for_a_new_one(ads):
    first = _add(1, "IT-supportkonsulent", "ECIT Solutions", "Aarhus", _text("support"))
    db.update("jobs", first, {"status": "filtered", "notes": "Skipped: too far."})
    ads.same = {"same": 1, "reason": "Same."}
    second = _add(2, "IT-supportkonsulent til Managed Service Provider", "ECIT Solutions", "Aarhus", _text("helpdesk"), "jobnet")
    assert jobs.get(second)["status"] == "new" and _asked(ads) == []


def test_a_job_you_added_yourself_is_never_checked(ads):
    _add(1, "IT-supportkonsulent", "ECIT Solutions", "Aarhus", _text("support"))
    ads.same = {"same": 1, "reason": "Same."}
    job_id = jobs.add_from_text("TITLE=IT-supportkonsulent til MSP. COMPANY=ECIT Solutions. LOC=Aarhus. " + _text("mine"))
    jobs.parse(job_id, use_text=True)
    assert jobs.get(job_id)["duplicate_of"] is None and _asked(ads) == []
