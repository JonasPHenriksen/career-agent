"""Kinds of job the user doesn't want, duplicate ads, and drafting the best matches first."""
from __future__ import annotations

import re

import pytest

from career_agent import db, jobkinds, jobs, profile, settings, sources, tasks

from .conftest import JOB_TEXT


# ---- kind_of: title, employment type and level; Danish and English ----------------------------------------------

@pytest.mark.parametrize("title, kind", [
    ("Student Assistant, Marketing", "student"), ("Studentermedhjælper til HR", "student"),
    ("Studiejob som webudvikler", "student"), ("Working student – data", "student"),
    ("Studentermedarbejder i kundeservice", "student"), ("Student Affairs Officer", "student"),
    ("Marketing Intern", "internship"), ("Summer internship 2027", "internship"), ("Praktikant i økonomi", "internship"),
    ("Praktikophold hos os", "internship"), ("Intern - Finance", "internship"), ("Praktik som UX-designer", "internship"),
    ("Part-time barista", "part_time"), ("Deltidsstilling som bogholder", "part_time"), ("Part time receptionist", "part_time"),
    ("Timeansat kundeservicemedarbejder", "part_time"),
    ("Barselsvikar til salgsafdelingen", "temporary"), ("Vikariat som lærer", "temporary"),
    ("Tidsbegrænset stilling som projektleder", "temporary"), ("Temporary Accountant", "temporary"),
    ("Fixed-term Project Manager", "temporary"), ("Interim manager", "temporary"),
    ("Project assistant, 6 month contract", "temporary"), ("Maternity cover - HR partner", "temporary"),
    ("Freelance copywriter", "freelance"), ("Freelancer til video", "freelance"), ("IT Contractor", "freelance"),
    ("PhD Fellow in Machine Learning", "research"), ("Ph.d.-stipendiat i økonomi", "research"),
    ("Postdoc in Physics", "research"), ("Associate Professor of Law", "research"), ("Adjunkt i biologi", "research"),
    ("Lektor i matematik", "research"), ("Research Assistant", "research"),
    ("Frivillig i genbrugsbutik", "volunteer"), ("Volunteer Driver", "volunteer"), ("Unpaid content writer", "volunteer"),
])
def test_kind_of_title(title, kind):
    assert jobkinds.kind_of(title) == kind


@pytest.mark.parametrize("title", [
    "International Sales Manager", "Internal Auditor", "Intern revisor", "Intern kommunikation", "Associate Director",
    "Senior Associate, Tax", "Graduate Programme 2027", "Trainee i banken", "Management Trainee",
    "Part of a great team: Developer", "Partner Manager", "Product Owner", "Full stack developer",
    "Professional Services Consultant", "Volunteer Coordinator", "Frivilligkoordinator", "Praktisk assistent",
    "Studievejleder", "Vejleder for studerende", "Customer Success Manager", "Temperature Control Engineer",
    "Interior Designer", "Contract Manager", "Template Designer", "", None,
])
def test_kind_of_ordinary_titles_are_not_flagged(title):
    assert jobkinds.kind_of(title) is None


def test_kind_of_uses_the_read_ads_type_and_level():
    assert jobkinds.kind_of("Developer", employment_type="part-time") == "part_time"
    assert jobkinds.kind_of("Developer", employment_type="Deltid") == "part_time"
    assert jobkinds.kind_of("Developer", employment_type="Temporary") == "temporary"
    assert jobkinds.kind_of("Developer", employment_type="tidsbegrænset") == "temporary"
    assert jobkinds.kind_of("Developer", employment_type="Freelance / contractor") == "freelance"
    assert jobkinds.kind_of("Developer", employment_type="internship") == "internship"
    assert jobkinds.kind_of("Developer", seniority="student") == "student"
    # mixed or ordinary values don't count
    assert jobkinds.kind_of("Developer", employment_type="full-time or part-time") is None
    assert jobkinds.kind_of("Developer", employment_type="Fuld- eller deltid") is None
    assert jobkinds.kind_of("Developer", employment_type="permanent, full-time") is None
    assert jobkinds.kind_of("Developer", employment_type="full-time", seniority="senior") is None
    assert jobkinds.kind_of("Developer", employment_type="", seniority="unknown") is None
    # the title wins by order in KINDS when several apply
    assert jobkinds.kind_of("Student assistant", employment_type="part-time") == "student"
    assert jobkinds.kinds_of("Student assistant", employment_type="part-time") == ["student", "part_time"]


def test_kind_of_ignores_body_text_except_unmistakable_phrases():
    assert jobkinds.kind_of("Developer", "We also employ student workers and interns. Part-time is possible.") is None
    assert jobkinds.kind_of("Developer", "Vi tilbyder også studiejob og deltid.") is None
    assert jobkinds.kind_of("Developer", "Dette er et barselsvikariat på 10 måneder.") == "temporary"
    assert jobkinds.kind_of("Developer", "This is an unpaid internship.") == "internship"


# ---- unwanted(): follows the toggles --------------------------------------------------------------------------

def test_unwanted_follows_the_toggles(fake):
    assert jobkinds.unwanted("Student Assistant") is None, "everything is wanted by default"
    settings.update("profile", {"job_kinds": {"student": False}})
    assert jobkinds.unwanted("Student Assistant") == ("student", "student job")
    assert jobkinds.unwanted("Developer", employment_type="part-time") is None, "other kinds stay on"
    assert jobkinds.unwanted("Marketing Intern") is None
    settings.update("profile", {"job_kinds": {"student": False, "part_time": False}})
    assert jobkinds.unwanted("Student assistant, part-time")[0] == "student"
    assert jobkinds.unwanted("Cashier", employment_type="part-time") == ("part_time", "part-time job")
    # off only for the kind that is off: a student job that is also part-time when only part-time is off
    settings.update("profile", {"job_kinds": {"student": True, "part_time": False}})
    assert jobkinds.unwanted("Student assistant", employment_type="part-time")[0] == "part_time"
    assert jobkinds.skip_note("student job") == \
        "Skipped: student job. You've switched these off under Profile → About you."


def test_kinds_switched_off_are_told_to_the_models(fake):
    assert "Not interested in" not in settings.get("profile").get("name", "") + _block()
    settings.update("profile", {"job_kinds": {"student": False, "internship": False, "part_time": False}})
    assert "- Not interested in: student job, internship, part-time job" in _block()


def _block() -> str:
    from career_agent import profile
    return profile.candidate_block()


def test_about_you_form_saves_the_kinds(fake):
    from fastapi.testclient import TestClient
    from career_agent.web.app import app
    client = TestClient(app)
    page = client.get("/profile?tab=about").text
    assert "Kinds of job to look at" in page and page.count('name="kind_') == 7
    r = client.post("/profile/about", data={"name": "Sara", "kind_internship": "1", "kind_volunteer": "1"},
                    follow_redirects=False)
    assert r.status_code in (302, 303)
    assert settings.get("profile")["job_kinds"] == {
        "student": False, "internship": True, "part_time": False, "temporary": False, "freelance": False,
        "research": False, "volunteer": True}


# ---- helpers for the pipeline tests ----------------------------------------------------------------------------

def _facts() -> None:
    db.insert("facts", {"category": "skill", "title": "Scrum", "created_at": db.now()})


def _model(fake, title="Product Owner", employment="full-time", seniority="senior", score=86):
    """Make the fake model read every ad as the given job; a score can be put in the title as 'SCORE=nn'."""
    base = fake.answer

    def answer(system, user, schema):
        data = base(system, user, schema)
        if schema is jobs.PARSE_SCHEMA:
            m = re.search(r"TITLE=([^\n.]*)", user)
            data.update(title=(m.group(1).strip() if m else title), employment_type=employment, seniority=seniority,
                        deadline="")
        elif schema is jobs.FIT_SCHEMA:
            m = re.search(r"Title: .*?SCORE=(\d+)", user)
            data["score"] = int(m.group(1)) if m else score
        return data
    fake.answer = answer


def _source_job(text: str = JOB_TEXT, n: int = 1) -> int:
    source_id = db.q1("SELECT id FROM sources")["id"] if db.q1("SELECT id FROM sources") else sources.add("Test", "https://example.com/jobs")
    job_id, _ = jobs.add_from_url(f"https://example.com/job/{n}", source_id=source_id, title="x", text=text)
    return job_id


def _scoring_calls(fake) -> int:
    return sum(1 for _, _, schema in fake.calls if schema is jobs.FIT_SCHEMA)


# ---- skipping unwanted kinds without model calls ---------------------------------------------------------------

def test_parse_skips_a_switched_off_kind_and_queues_no_scoring(fake):
    _facts()
    _model(fake, employment="part-time")
    settings.update("profile", {"job_kinds": {"part_time": False}})
    job_id = _source_job()
    out = jobs.parse(job_id, use_text=True)
    job = jobs.get(job_id)
    assert out["skipped"] == "part-time job"
    assert job["status"] == "filtered" and job["notes"] == jobkinds.skip_note("part-time job")
    assert not db.q1("SELECT id FROM tasks WHERE kind = 'score_job'")
    assert _scoring_calls(fake) == 0


def test_parse_skips_by_the_read_level_too(fake):
    _model(fake, seniority="student")
    settings.update("profile", {"job_kinds": {"student": False}})
    job_id = _source_job()
    jobs.parse(job_id, use_text=True)
    assert jobs.get(job_id)["status"] == "filtered"


def test_a_wanted_kind_is_scored_as_usual(fake):
    _model(fake, employment="part-time")
    job_id = _source_job()
    jobs.parse(job_id, use_text=True)
    assert jobs.get(job_id)["status"] == "new" and db.q1("SELECT id FROM tasks WHERE kind = 'score_job'")


def test_a_job_added_by_hand_is_never_skipped(fake):
    _facts()
    _model(fake, employment="part-time", seniority="student")
    settings.update("profile", {"job_kinds": {k: False for k, _ in jobkinds.KINDS}})
    job_id = jobs.add_from_text(JOB_TEXT)
    jobs.parse(job_id, use_text=True)
    assert jobs.get(job_id)["status"] == "new" and db.q1("SELECT id FROM tasks WHERE kind = 'score_job'")
    assert jobs.score(job_id)["score"] == 86 and _scoring_calls(fake) == 1


def test_score_skips_jobs_read_before_the_setting_changed(fake):
    _facts()
    _model(fake, employment="part-time")
    job_id = _source_job()
    jobs.parse(job_id, use_text=True)
    settings.update("profile", {"job_kinds": {"part_time": False}})
    assert jobs.score(job_id) == {"skipped": "part-time job"}
    assert jobs.get(job_id)["status"] == "filtered"
    assert _scoring_calls(fake) == 0


def test_score_does_not_skip_a_job_that_already_has_an_application(fake):
    _facts()
    _model(fake, employment="part-time")
    job_id = _source_job()
    jobs.parse(job_id, use_text=True)
    jobs.create_application(job_id, draft=False)
    settings.update("profile", {"job_kinds": {"part_time": False}})
    assert jobs.score(job_id)["score"] == 86 and _scoring_calls(fake) == 1


# ---- the same ad posted several times --------------------------------------------------------------------------

LONG_AD = " ".join(f"word{i % 97} line{i} of the full stack developer ad about our stack and team" for i in range(400))


def test_same_ad_text_near_identical_and_clearly_different():
    assert len(LONG_AD) > 2500
    tweaked = LONG_AD.replace("line10 ", "line10 extra ").replace("Nic", "Group") + " Apply today."
    assert jobs.same_ad_text(LONG_AD, tweaked)
    different = " ".join(f"other{i} piece{i * 3} about accounting and payroll duties" for i in range(400))
    assert not jobs.same_ad_text(LONG_AD, different)
    assert not jobs.same_ad_text(LONG_AD, "")


def test_a_teaser_counts_only_when_contained_in_the_other():
    teaser = " ".join(LONG_AD.split()[:150])
    assert len(teaser) < 2500
    assert jobs.same_ad_text(teaser, LONG_AD), "the teaser is the start of the full ad"
    intro = "We are a family company founded in 1950 with offices in Aarhus and a warm, open culture. " * 2
    one = intro + " ".join(f"alpha{i} sales task{i}" for i in range(60))
    two = intro + " ".join(f"beta{i} support duty{i}" for i in range(60))
    assert len(one) < 2500 and not jobs.same_ad_text(one, two), "a shared company intro is not the same job"


def test_the_same_ad_under_a_sister_company_is_filtered_and_not_scored(fake):
    _facts()
    _model(fake)
    first, second = _source_job("TITLE=Full stack developer. " + LONG_AD, 1), None
    second = _source_job("British Car Import is hiring. " + "TITLE=Full stack developer. " + LONG_AD, 2)
    db.update("jobs", first, {"company_name": "Nic. Christiansen Gruppen"})
    jobs.parse(first, use_text=True)
    db.update("jobs", first, {"company_name": "Nic. Christiansen Gruppen"})
    jobs.parse(second, use_text=True)
    job = jobs.get(second)
    assert job["status"] == "filtered"
    assert job["duplicate_of"] == first and "same title and almost the same ad text" in job["notes"]
    assert db.scalar("SELECT COUNT(*) FROM tasks WHERE kind = 'score_job'") == 1, "only the first ad is scored"
    # a dismissed original no longer hides new copies
    third = _source_job("TITLE=Full stack developer. " + LONG_AD, 3)
    jobs.set_status(first, "dismissed")
    jobs.set_status(second, "dismissed")
    jobs.parse(third, use_text=True)
    assert jobs.get(third)["status"] == "new"


def test_a_different_ad_with_the_same_title_is_kept(fake):
    _model(fake)
    one = _source_job("TITLE=Full stack developer. " + LONG_AD, 1)
    other = " ".join(f"other{i} piece{i * 3} about accounting and payroll duties" for i in range(400))
    two = _source_job("TITLE=Full stack developer. " + other, 2)
    jobs.parse(one, use_text=True)
    jobs.parse(two, use_text=True)
    assert jobs.get(two)["status"] == "new"


def test_hand_added_ads_are_never_called_duplicates(fake):
    _model(fake)
    one = jobs.add_from_text(JOB_TEXT)
    jobs.parse(one, use_text=True)
    two = jobs.add_from_text(JOB_TEXT + "\n")
    jobs.parse(two, use_text=True)
    assert jobs.get(two)["status"] == "new"


# ---- draft the best matches first -------------------------------------------------------------------------------

def _scored_jobs(fake, scores: list[int], score: bool = True) -> list[int]:
    """Read (and by default score, without drafting) one job per score; the score travels in the title."""
    _model(fake)
    ids = []
    for n, s in enumerate(scores, 1):
        text = f"Different job number {n} unique{n} " + " ".join(f"t{n}x{i}" for i in range(50)) + f" TITLE=Job {n} SCORE={s}"
        ids.append(jobs.add_from_text(text))
        jobs.parse(ids[-1], use_text=True)
    db.ex("DELETE FROM tasks WHERE kind = 'parse_job'")  # we ran the reading ourselves
    if score:
        db.ex("DELETE FROM tasks")
        wanted = settings.get("automation")["auto_draft"]
        settings.update("automation", {"auto_draft": False})
        for job_id in ids:
            jobs.score(job_id)
        settings.update("automation", {"auto_draft": wanted})
    return ids


def test_drafting_waits_for_the_batch_and_takes_the_best(fake):
    _facts()
    settings.update("automation", {"auto_draft": True, "auto_draft_threshold": 80, "auto_draft_daily_cap": 2})
    ids = _scored_jobs(fake, [82, 84, 92, 90, 70], score=False)
    queue = db.q("SELECT * FROM tasks WHERE kind = 'score_job' ORDER BY id")
    assert len(queue) == 5
    for t in queue[:-1]:  # the other jobs are still waiting, so nothing is drafted yet
        jobs.score(db.jload(t["payload"])["job_id"])
        db.update("tasks", t["id"], {"status": "done"})
        assert db.scalar("SELECT COUNT(*) FROM applications") == 0 or t is queue[-2]
    # the last score_job is still queued (its own task is excluded), so the batch is complete
    jobs.score(db.jload(queue[-1]["payload"])["job_id"])
    drafted = db.q("SELECT job_id FROM applications WHERE auto_drafted = 1 ORDER BY id")
    assert [r["job_id"] for r in drafted] == [ids[2], ids[3]], "the 92 and the 90, not the first to arrive"


def test_drafting_does_nothing_while_other_jobs_are_still_being_read(fake):
    _facts()
    settings.update("automation", {"auto_draft": True})
    [job_id] = _scored_jobs(fake, [95])
    db.ex("DELETE FROM applications")
    tasks.enqueue("parse_job", {"job_id": 9999}, ref="job:9999")
    db.ex("DELETE FROM applications")
    assert jobs.maybe_auto_draft(job_id) is False
    assert db.scalar("SELECT COUNT(*) FROM applications") == 0
    db.ex("DELETE FROM tasks")
    assert jobs.maybe_auto_draft(job_id) is True


def test_drafting_ties_go_to_the_nearest_deadline_and_respect_what_is_left_of_the_limit(fake):
    _facts()
    settings.update("automation", {"auto_draft": True, "auto_draft_daily_cap": 3})
    ids = _scored_jobs(fake, [90, 90, 90, 85])
    db.ex("DELETE FROM tasks")
    db.update("jobs", ids[0], {"deadline": "2031-03-01"})
    db.update("jobs", ids[1], {"deadline": "2031-01-01"})
    db.update("jobs", ids[2], {"deadline": None})
    first = jobs.create_application(ids[3], auto=True, draft=False)  # one of today's three is already used
    assert first
    assert jobs.draft_best() == [ids[1], ids[0]], "the two left: nearest deadline first, no deadline last"
    assert jobs.draft_best() == [], "the limit is used up"


def test_drafting_skips_blocked_companies_and_past_deadlines(fake):
    _facts()
    settings.update("automation", {"auto_draft": True})
    ids = _scored_jobs(fake, [95, 94, 93])
    db.ex("DELETE FROM tasks")
    db.update("jobs", ids[0], {"deadline": "2020-01-01"})
    db.ex("UPDATE companies SET blocked = 1")
    assert jobs.draft_best() == []
    db.ex("UPDATE companies SET blocked = 0")
    assert jobs.draft_best() == [ids[1], ids[2]]


def test_distance_from_home_is_measured_not_guessed(fake):
    from career_agent import places

    def km(where):
        return places.reach(where, "Horsens", 50)

    assert km("Aarhus C")["km"] == 40 and km("Aarhus C")["inside"] is True
    assert km("Gothersgade 20, 7000 Fredericia")["town"] == "Fredericia", "a street address is not a place of its own"
    assert km("Odense")["inside"] is False and km("Odense")["km"] > 80, "over the bridge, not across the water"
    assert km("Copenhagen, Aarhus or Aalborg, Denmark")["inside"] is True, "one office within reach is enough"
    assert km("Leeds, United Kingdom")["inside"] is False and km("Leeds, United Kingdom")["town"] is None
    assert km("Herning (VELUX hovedsæde)")["inside"] is None, "part of the location is unclear: not ruled out"
    assert km("Sydøstjylland") is None and places.reach("Aarhus", "", 50) is None
    assert "Aarhus is 40 km from Horsens" in places.describe(km("Aarhus"), 50)

    wanted = settings.get("profile")
    settings.update("profile", {"city": "Horsens", "max_km": "50"})
    try:
        assert "too far" in jobs.too_far("Roskilde", "onsite") and "199 km" in jobs.too_far("Roskilde", "hybrid")
        assert jobs.too_far("Roskilde", "remote") is None and jobs.too_far("Roskilde", "unknown") is None
        assert jobs.too_far("Aarhus", "onsite") is None and jobs.too_far("Et sted i Jylland", "onsite") is None
        assert "Longest distance to on-site or hybrid work: 50 km from Horsens" in profile.candidate_block()

        # A far on-site job that was read earlier is set aside without asking a model; one you keep is scored.
        sid = db.insert("sources", {"name": "Feed", "kind": "rss", "url": "https://example.com/far", "created_at": db.now()})
        far = db.insert("jobs", {"title": "Backend developer", "url": "https://example.com/far/1", "status": "new",
                                 "parse_status": "parsed", "source_id": sid, "location": "Roskilde", "work_mode": "onsite",
                                 "description": "Backend developer in Roskilde.", "created_at": db.now(), "updated_at": db.now()})
        calls = len(fake.calls)
        assert jobs.score(far) == {"skipped": "too far"} and len(fake.calls) == calls
        row = jobs.get(far)
        assert row["status"] == "filtered" and row["notes"].startswith("Skipped: too far. On site in Roskilde, 199 km")
        assert "Distance from home (measured): Roskilde is 199 km from Horsens" in jobs.job_block(row)
        settings.update("profile", {"max_km": ""})
        assert jobs.too_far("Roskilde", "onsite") is None, "no limit set: nothing is ruled out"
    finally:
        settings.update("profile", {"city": wanted.get("city", ""), "max_km": wanted.get("max_km", "")})
