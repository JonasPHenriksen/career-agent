"""Which jobs come first: fit, less a few points for distance and big employers; and how soon to apply."""
from __future__ import annotations

from datetime import date, timedelta

from fastapi.testclient import TestClient

from career_agent import companies, db, jobs, rank, settings, tasks
from career_agent.web.app import app


def _home(preferred="25", limit="50", nudge="moderate"):
    settings.update("profile", {"city": "Horsens", "preferred_km": preferred, "max_km": limit, "big_employers": nudge})


def _job(title, company, location, fit, work_mode="onsite", size=None, **extra) -> int:
    cid = companies.upsert(company)
    if size:
        db.update("companies", cid, {"size": size, "size_by": "model"})
    return db.insert("jobs", {"url": f"https://example.com/{title}-{company}".replace(" ", "-"), "title": title,
                              "company_name": company, "company_id": cid, "location": location, "work_mode": work_mode,
                              "fit_score": fit, "status": "new", "parse_status": "parsed", "created_at": db.now(),
                              **extra})


def _rows():
    return db.q("SELECT * FROM jobs ORDER BY id")


def test_distance_costs_nothing_inside_the_preferred_distance_and_up_to_12_points_at_the_limit():
    assert rank.distance_points(20, "onsite", 25, 50) == 0
    assert rank.distance_points(25, "onsite", 25, 50) == 0
    assert rank.distance_points(40, "onsite", 25, 50) == 7, "Aarhus from Horsens"
    assert rank.distance_points(40, None, 25, 50) == 7, "work mode not stated counts as on site"
    assert rank.distance_points(40, "hybrid", 25, 50) == 4, "fewer days on the road"
    assert rank.distance_points(40, "remote", 25, 50) == 0
    assert rank.distance_points(50, "onsite", 25, 50) == 12
    assert rank.distance_points(70, "onsite", 25, 50) == 12, "a job kept beyond the limit costs no more"
    assert rank.distance_points(40, "onsite", None, 50) == 0, "no preference, no points"
    assert rank.distance_points(None, "onsite", 25, 50) == 0, "a place the app doesn't know"


def test_a_near_job_at_a_small_employer_beats_a_slightly_better_fit_at_a_big_one_further_away(fake):
    _home()
    aarhus = _job("Linux Operations Engineer", "Netcompany", "Aarhus", 72, size="very_large")
    horsens = _job("IT-supporter", "Lokal IT ApS", "Horsens", 66, size="small")
    vejle = _job("Developer", "Mellem A/S", "Vejle", 68, size="large")
    unscored = _job("Tester", "Lokal IT ApS", "Horsens", None)
    order = rank.best_first(_rows())
    assert [r["id"] for r in order] == [horsens, vejle, aarhus, unscored], "Vejle 68 at a large employer is 64"
    a = next(r for r in order if r["id"] == aarhus)["rank"]
    assert a["score"] == 72 - 7 - 8 and a["points"] == [(7, "Aarhus, 40 km"), (8, "very large or very well-known employer")]
    assert next(r for r in order if r["id"] == vejle)["rank"]["points"] == [(4, "large employer")]
    assert next(r for r in order if r["id"] == unscored)["rank"]["score"] is None

    settings.update("profile", {"big_employers": "strong"})
    assert rank.annotate([db.q1("SELECT * FROM jobs WHERE id = ?", (aarhus,))])[0]["rank"]["score"] == 72 - 7 - 16
    settings.update("profile", {"big_employers": "off", "preferred_km": ""})
    assert rank.annotate([db.q1("SELECT * FROM jobs WHERE id = ?", (aarhus,))])[0]["rank"]["score"] == 72


def test_a_job_without_a_date_is_urgent_unless_it_was_posted_long_ago_or_is_an_open_application():
    today = date(2026, 11, 22)
    fresh = rank.urgency({"title": "Developer", "posted_at": "2026-11-20", "details": '{"deadline_note": "snarest"}'}, today)
    assert fresh["kind"] == "rolling" and fresh["due"] == date(2026, 11, 27), "a week after it was posted"
    waiting = rank.urgency({"title": "Developer", "posted_at": "2026-11-05"}, today)
    assert waiting["kind"] == "rolling" and waiting["due"] == today + timedelta(days=2), "never sooner than two days"
    seen = rank.urgency({"title": "Developer", "created_at": "2026-11-18T10:00:00"}, today)
    assert seen["due"] == date(2026, 11, 25), "no posting date: from when it was found"
    old = rank.urgency({"title": "Developer", "posted_at": "2026-09-01"}, today)
    assert old["kind"] == "stale" and old["due"] is None and old["age_days"] == 82
    assert rank.urgency({"title": "Uopfordret ansøgning, TestHuset", "posted_at": "2026-11-20"}, today)["kind"] == "open"
    dated = rank.urgency({"title": "x", "deadline": "2026-12-10", "posted_at": "2026-11-20"}, today)
    assert dated["kind"] == "dated" and dated["due"] == date(2026, 12, 10)
    both = rank.urgency({"title": "x", "deadline": "2026-12-10", "posted_at": "2026-11-20",
                         "details": '{"deadline_note": "samtaler løbende"}'}, today)
    assert both["due"] == date(2026, 11, 27), "interviews as applications arrive: the job may go before the date"


def test_most_urgent_puts_rolling_jobs_among_the_deadlines_and_old_or_open_ones_last(fake):
    _home()
    today = date.today()
    far = _job("Far deadline", "A ApS", "Horsens", 70, deadline=(today + timedelta(days=30)).isoformat())
    tomorrow = _job("Due tomorrow", "B ApS", "Horsens", 60, deadline=(today + timedelta(days=1)).isoformat())
    rolling = _job("Rolling", "C ApS", "Horsens", 65, posted_at=today.isoformat())
    stale = _job("Stale", "D ApS", "Horsens", 90, posted_at=(today - timedelta(days=60)).isoformat())
    unsolicited = _job("Unsolicited application", "E ApS", "Horsens", 95)
    poor = _job("Poor match due today", "F ApS", "Horsens", 30, deadline=today.isoformat())
    order = [r["id"] for r in rank.urgent_first(_rows())]
    assert order == [tomorrow, rolling, far, stale, unsolicited, poor], "a poor match isn't worth hurrying for"


def test_dates_the_ad_never_states_are_when_the_listing_comes_down():
    text = "Ansøgningsfrist: 1. december. Vi glæder os til at høre fra dig."
    assert jobs.date_in_text("2026-12-01", text) and jobs.date_in_text("2026-12-01", "Deadline: December 1st")
    assert jobs.date_in_text("2026-12-01", "Send senest 01/12") and jobs.date_in_text("2026-12-01", "frist 2026-12-01")
    assert not jobs.date_in_text("2026-12-16", text)
    assert jobs.listing_date("2026-12-16", "snarest muligt", text), "apply soon + a date the ad never mentions"
    assert not jobs.listing_date("2026-12-01", "snarest muligt", text), "the ad states it: a real deadline"
    assert jobs.listing_date("2026-12-16", "", text, listing_ends="2026-12-16"), "the page's 'valid through'"
    assert not jobs.listing_date("2026-12-16", "", text), "no note, no valid-through: keep the date"


def test_reading_an_ad_keeps_the_boards_valid_through_date_apart_from_the_deadline(fake, monkeypatch):
    from career_agent import fetch

    page = ('<html><head><script type="application/ld+json">{"@type": "JobPosting", "title": "Product Owner", '
            '"validThrough": "2030-12-16", "description": "Product Owner hos FinPay. Søg snarest."}</script></head>'
            '<body><h1>Product Owner</h1><p>Product Owner hos FinPay. Søg snarest, vi holder samtaler løbende.</p>'
            '</body></html>')

    class Resp:
        text, url = page, "https://finpay.example/job/1"

    monkeypatch.setattr(fetch, "ad_page", lambda url: Resp())
    base = fake.answer
    fake.answer = lambda system, user, schema: ({**base(system, user, schema), "deadline": "2030-12-16",
                                                 "deadline_note": "snarest"} if schema is jobs.PARSE_SCHEMA
                                                else base(system, user, schema))
    job_id, _ = jobs.add_from_url("https://finpay.example/job/1")
    jobs.parse(job_id)
    job = jobs.get(job_id)
    assert job["deadline"] is None and job["d"]["listing_ends"] == "2030-12-16"
    assert jobs.closes_on(job) == "2030-12-16" and not jobs.is_expired(job)
    assert rank.urgency(job)["kind"] == "rolling"
    assert db.scalar("SELECT COUNT(*) FROM tasks WHERE kind = 'size_companies' AND run_after IS NOT NULL") == 1, \
        "the new employer is judged a little later, together with others found meanwhile"


def test_jobs_read_before_keep_their_real_deadlines_and_lose_the_listing_dates(fake):
    today = date.today()
    real = _job("Real", "A ApS", "Horsens", 70, deadline="2030-12-01", description="Ansøgningsfrist 1. december 2030",
                details='{"deadline_note": "samtaler løbende"}')
    listing = _job("Listing", "B ApS", "Horsens", 70, deadline="2030-12-16", description="Søg snarest muligt",
                   details='{"deadline_note": "snarest muligt"}')
    gone = _job("Gone", "C ApS", "Horsens", 70, deadline=(today - timedelta(days=1)).isoformat(),
                description="Søg snarest", details='{"deadline_note": "snarest"}')
    assert jobs.tidy_after_update()["listing_dates"] == 2
    assert jobs.tidy_after_update() == {}, "only once"
    assert jobs.get(real)["deadline"] == "2030-12-01"
    assert jobs.get(listing)["deadline"] is None and jobs.get(listing)["d"]["listing_ends"] == "2030-12-16"
    open_ids = {r["id"] for r in db.q(f"SELECT id FROM jobs j WHERE {jobs.open_sql()}", (today.isoformat(),))}
    assert open_ids == {real, listing}, "a listing that came down is gone, like a passed deadline"


def test_the_same_job_with_its_title_written_two_ways():
    assert jobs.same_title("Linux Operations Engineer", "Linux Operations Engineer: Copenhagen, Aarhus or Aalborg")
    assert jobs.same_title("Field Service Engineer, Critical Infrastructure Protection",
                           "Field Service Engineer, Critical Infrastructure Protection, Lystrup")
    assert jobs.same_title("Entry IT Specialist, Power BI & Data Engineering - Aarhus",
                           "Entry IT Specialist, Power BI & Data Engineering")
    assert jobs.same_title("Backendudvikler til KMD Sekoia", "Backend-udvikler til KMD Sekoia")
    assert jobs.same_title("AI-udvikler / AI Engineer", "AI-udvikler / AI Engineer til Krifa", "Krifa",
                           "Kristelig Fagbevægelse (KRIFA)")
    assert jobs.same_title("Platform Engineer", "Platform Engineer at Whiteaway Group", "WhiteAway Group", "Whiteaway Group")
    assert not jobs.same_title("IT Consultant", "IT Consultant, Netcompany Consulting", "Netcompany", "Netcompany A/S"), \
        "a business unit is another job"
    assert not jobs.same_title("Software Engineer", "Software Engineer - Platform")
    assert not jobs.same_title("Linux Operations Engineer", "Microsoft Operations Engineer")
    assert not jobs.same_title("IT-supporter", "IT-supporter med lyst til mennesker")


def test_one_company_by_its_short_name_or_cut_short():
    assert jobs.same_company("Krifa", "Kristelig Fagbevægelse (KRIFA)")
    assert jobs.same_company("Brdr. A&O J...", "Brdr. A&O Johansen A/S")
    assert jobs.same_company("TestHuset (Trifork)", "Trifork")
    assert not jobs.same_company("Brdr. A&O Johansen A/S (via Moment)", "Moment A/S"), "the recruiter isn't the employer"
    assert not jobs.same_company("Krifa", "Kristelig Fagbevægelse")


def test_the_sweep_sets_aside_a_job_found_twice_under_two_spellings(fake):
    a = db.insert("sources", {"name": "netcompany.com", "kind": "rss", "url": "https://a.example", "created_at": db.now()})
    b = db.insert("sources", {"name": "jobindex", "kind": "rss", "url": "https://b.example", "created_at": db.now()})
    first = db.insert("jobs", {"url": "https://a.example/1", "title": "Linux Operations Engineer", "company_name": "Netcompany",
                               "location": "Copenhagen, Aarhus", "source_id": a, "status": "new", "created_at": db.now()})
    second = db.insert("jobs", {"url": "https://b.example/1", "title": "Linux Operations Engineer: Copenhagen, Aarhus or Aalborg",
                                "company_name": "Netcompany A/S", "location": "Aarhus", "source_id": b, "status": "new",
                                "created_at": db.now()})
    other = db.insert("jobs", {"url": "https://b.example/2", "title": "Microsoft Operations Engineer", "company_name": "Netcompany A/S",
                               "location": "Aarhus", "source_id": b, "status": "new", "created_at": db.now()})
    assert jobs.mark_existing_repeats() == 1
    assert db.q1("SELECT duplicate_of FROM jobs WHERE id = ?", (second,))["duplicate_of"] == first
    assert db.q1("SELECT duplicate_of FROM jobs WHERE id = ?", (other,))["duplicate_of"] is None


def test_employer_sizes_are_judged_in_batches_and_yours_wins(fake):
    _home()
    big = _job("A", "Netcompany", "Aarhus", 70)
    mid = _job("B", "Mellem A/S", "Vejle", 70)
    skipped = _job("C", "Ukendt Firma ApS", "Horsens", 70)
    mine = _job("D", "Min Virksomhed ApS", "Horsens", 70)
    _job("E", "Gammel ApS", "Horsens", 70, status="dismissed")
    companies.set_size(db.q1("SELECT company_id FROM jobs WHERE id = ?", (mine,))["company_id"], "large")
    assert [c["name"] for c in companies.unsized()] == ["Netcompany", "Mellem A/S", "Ukendt Firma ApS"], \
        "only employers of jobs you might still apply to, and not one you judged yourself"
    assert companies.judge_sizes() == {"judged": 3}
    size = {r["title"]: (r["size"], r["size_by"]) for r in db.q(
        "SELECT j.title, c.size, c.size_by FROM jobs j JOIN companies c ON c.id = j.company_id")}
    assert size["A"] == ("very_large", "model") and size["B"] == ("medium", "model")
    assert size["C"] == ("unknown", "model"), "an employer the model skipped is unknown, not asked again and again"
    assert size["D"] == ("large", "you")
    assert companies.unsized() == []

    settings.update("profile", {"big_employers": "off"})
    assert not companies.sizes_wanted()
    assert big and mid and skipped


def test_the_pages_show_the_order_and_why(fake):
    _home()
    _job("Linux Operations Engineer", "Netcompany", "Aarhus", 72, size="very_large", posted_at=date.today().isoformat())
    _job("IT-supporter", "Lokal IT ApS", "Horsens", 66, size="small", deadline="2030-01-01")
    c = TestClient(app)
    page = c.get("/jobs").text
    assert page.index("IT-supporter") < page.index("Linux Operations Engineer")
    assert "Ranked 57: −7 Aarhus, 40 km, −8 very large or very well-known employer" in page
    assert "rolling · apply soon" in page
    urgent = c.get("/jobs?sort=urgent").text
    assert urgent.index("Linux Operations Engineer") < urgent.index("IT-supporter")
    assert c.get("/jobs?sort=deadline").status_code == 200, "an old link to the deadline order still works"
    r = c.post("/profile/about", data={"city": "Horsens", "max_km": "50", "preferred_km": "25", "big_employers": "loud"},
               follow_redirects=False)
    assert r.status_code == 303 and settings.get("profile")["big_employers"] == "moderate"
    cid = db.q1("SELECT id FROM companies WHERE name = 'Netcompany'")["id"]
    c.post(f"/companies/{cid}", data={"size": "medium"}, follow_redirects=False)
    assert db.q1("SELECT size, size_by FROM companies WHERE id = ?", (cid,)) == {"size": "medium", "size_by": "you"}
    assert "Ranked 65: −7 Aarhus, 40 km" in c.get("/jobs").text
    c.post(f"/companies/{cid}", data={"size": ""}, follow_redirects=False)
    assert db.q1("SELECT size FROM companies WHERE id = ?", (cid,))["size"] is None
    assert db.scalar("SELECT COUNT(*) FROM tasks WHERE kind = 'size_companies'") >= 1, "handed back to the model"
    assert tasks.TASK_LABELS["size_companies"]
