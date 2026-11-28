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


def _source(name, kind="rss", status="10 listings, 2 new, 1 kept for review", error=None, enabled=1) -> int:
    return db.insert("sources", {"name": name, "kind": kind, "url": f"https://example.com/{name}", "enabled": enabled,
                                 "last_status": status, "last_error": error, "created_at": db.now()})


def _source_found(key):
    return [i["title"] for c in health.source_checks() if c["key"] == key for i in c["items"]]


def test_the_source_checks_find_sources_that_broke_without_a_word(fake):
    from datetime import datetime, timedelta

    _source("Fine")
    _source("Fails", status="error", error="The site took too long to answer.")
    _source("Switched off", status="error", error="Robots not allowed.", enabled=0)
    _source("Empty", kind="page", status="0 listings, 0 new, 0 kept for review")
    _source("Yours to run", kind="collect", status="0 listings, 0 new, 0 kept for review")
    quiet = _source("Quiet")
    old = (datetime.now() - timedelta(days=health.QUIET_DAYS + 2)).isoformat(timespec="seconds")
    db.update("jobs", _job("Old find", "Horsens", 60), {"source_id": quiet, "created_at": old})
    blind = _source("Blind")
    for i in range(health.FIELDS_MIN):
        db.update("jobs", _job(f"Blind {i}", "" if i else "Horsens", 60), {"source_id": blind})
    assert _source_found("source_error") == ["Fails"], "a source you switched off isn't a problem"
    assert _source_found("source_empty") == ["Empty"], "bookmark collection is yours to run"
    assert _source_found("source_quiet") == ["Quiet"]
    assert _source_found("source_fields") == ["Blind"], "4 of 5 new jobs without a place"
    assert "1 × a source that fails" in health.summary()
    page = TestClient(app).get("/checks").text
    assert 'id="sources"' in page and 'href="/settings#sources">Empty</a>' in page


def test_a_source_review_comes_back_every_four_weeks(fake):
    from datetime import date

    from career_agent import todos

    health.remind_review(date(2026, 12, 1))
    first = db.q1("SELECT * FROM todos WHERE check_kind = 'sources'")
    assert first["title"] == health.REVIEW_TITLE and first["show_from"] == "2026-12-29" and "Claude Code" in first["detail"]
    health.remind_review(date(2026, 12, 2))
    assert db.scalar("SELECT COUNT(*) FROM todos WHERE check_kind = 'sources'") == 1, "one at a time"
    assert todos.progress(first) == "No source problems right now"
    _source("Fails", status="error", error="Gone.")
    assert todos.progress(first) == "1 source problem under Data checks right now"
    todos.set_done(first["id"])
    db.update("todos", first["id"], {"done_at": "2027-01-03T10:00:00"})
    health.remind_review(date(2027, 1, 3))
    nxt = db.q1("SELECT * FROM todos WHERE check_kind = 'sources' AND done_at IS NULL")
    assert nxt["show_from"] == "2027-01-31", "four weeks after the last review"
