"""Holes in the job list: rule checks for mistakes found before, and a weekly model check for new kinds."""
from __future__ import annotations

from fastapi.testclient import TestClient

from career_agent import companies, db, health, jobs, llm, settings, tasks
from career_agent.web.app import app

AD = "Vi søger en udvikler til vores team i Horsens. " * 20


def _job(title, location, score=None, work_mode="onsite", **extra) -> int:
    return db.insert("jobs", {"url": f"https://example.com/{title}".replace(" ", "-"), "title": title,
                              "company_name": "Havbro", "company_id": companies.upsert("Havbro"), "location": location,
                              "work_mode": work_mode, "fit_score": score, "status": "new", "parse_status": "parsed",
                              "description": AD, "created_at": db.now(), **extra})


def _found(key):
    return [i["id"] for c in health.checks() if c["key"] == key for i in c["items"]]


def test_the_rule_checks_find_each_hole_found_before(fake):
    settings.update("profile", {"city": "Horsens", "max_km": "50"})
    fine = _job("Fine", "Hedensted", 70)
    no_place = _job("No place", "", 70)
    _job("Remote", "", 70, work_mode="remote")
    region = _job("Region", "Region Midtjylland", 70)
    teaser = _job("Teaser", "Horsens", 62, description="Krüger Search søger en IT-udvikler.")
    unread = _job("Unread", "Horsens", 40, parse_status="error", parse_error="This page doesn't look like a single job ad.")
    old_cap = _job("Old cap", "Skanderborg", 36, fit='{"dealbreakers": ["Skanderborg may be more than 50 km away"]}')
    talks = _job("Talks", "Aarhus", 70, fit='{"basis": 2, "summary": "A good match, and Aarhus is 40 km away."}')
    old_talk = _job("Old talk", "Aarhus", 66, fit='{"summary": "Aarhus is inside your limit, and you know Python."}')
    _job("Old quiet", "Aarhus", 66, fit='{"summary": "You know Python."}')
    assert _found("no_place") == [no_place], "a remote job needs no place"
    assert _found("unmeasured") == [region]
    assert _found("teaser") == [teaser]
    assert _found("scored_unread") == [unread]
    assert sorted(_found("place_in_fit")) == sorted([old_cap, talks, old_talk]), "an old fit that keeps quiet is fine"
    assert _found("work_mode") == [], "the ads say how the work is done"
    assert fine not in sum((c["items"] for c in health.checks()), []) and "1 × no place" in health.summary()

    c = TestClient(app)
    assert "Holes in your job list" in c.get("/").text and "1 × no place" in c.get("/").text
    page = c.get("/checks").text
    assert f'href="/jobs/{no_place}"' in page and "Region Midtjylland" in page

    db.ex("DELETE FROM jobs WHERE id != ?", (fine,))
    assert health.summary() == [] and "Holes in your job list" not in c.get("/").text


def test_most_new_ads_without_a_work_mode_looks_like_a_reading_problem(fake):
    for i in range(10):
        _job(f"Job {i}", "Horsens", 70, work_mode="unknown" if i < 7 else "onsite")
    assert len(_found("work_mode")) == 7
    db.ex("UPDATE jobs SET work_mode = 'hybrid' WHERE title IN ('Job 0', 'Job 1')")
    assert _found("work_mode") == [], "half is how ads are; most is a hole"


def test_the_weekly_check_compares_ads_with_what_was_stored(fake):
    wrong = _job("Wrong place", "København", 70)
    _job("Right", "Horsens", 70)

    db.ex("UPDATE jobs SET fit = ? WHERE id = ?", ('{"basis": 2, "summary": "You know Python."}', wrong))

    def answer(system, user, schema):
        assert schema is health.AUDIT_SCHEMA and "<ad>" in user
        assert ("fit_summary" in user) == ("Wrong place" in user), "an older fit's summary is a rule check's job"
        if "Wrong place" in user:
            return {"full_ad": True, "issues": [{"field": "location", "stored": "København", "ad_says": "Horsens",
                                                 "severity": "major"}]}
        return {"full_ad": True, "issues": []}

    fake.answer = answer
    assert llm.STEP_OF["audit_ad"] == "audit", "follows the Analysis picker like every other step"
    assert health.audit_due() and settings.get("automation")["weekly_audit"]
    assert health.audit() == {"checked": 2, "with_issues": 1}
    last = health.audit_result()
    assert last["findings"][0]["job_id"] == wrong and last["findings"][0]["issues"][0]["ad_says"] == "Horsens"
    assert not health.audit_due(), "once a week"
    assert "weekly check: 1 of 2 ads read wrong" in health.summary() and health.clean_weeks() == 0

    fake.answer = lambda system, user, schema: {"full_ad": True, "issues": []}
    for _ in range(health.CLEAN_WEEKS):
        health.audit()
    assert health.clean_weeks() == health.CLEAN_WEEKS
    c = TestClient(app)
    assert "you can turn it off" in c.get("/checks").text
    c.post("/checks/audit/switch", data={"on": "0"}, follow_redirects=False)
    assert not settings.get("automation")["weekly_audit"] and not health.audit_due()
    c.post("/checks/audit", follow_redirects=False)
    assert db.q1("SELECT id FROM tasks WHERE kind = 'audit_jobs' AND status = 'queued'"), "you can still run it by hand"
    assert tasks.TASK_STEPS["audit_jobs"] == ("audit",) and tasks.TASK_LABELS["audit_jobs"]
    assert jobs.MIN_AD_CHARS == 600
