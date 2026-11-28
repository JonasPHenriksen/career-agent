"""Which jobs come first: fit, less points for distance, the kind of job and deal-breakers; and how soon to apply."""
from __future__ import annotations

from datetime import date, timedelta

from fastapi.testclient import TestClient

from career_agent import companies, db, jobs, rank, settings, tasks
from career_agent.web.app import app


def _home(limit="50", nudge="off"):
    settings.update("profile", {"city": "Horsens", "max_km": limit, "big_employers": nudge})


def _job(title, company, location, score, work_mode="onsite", size=None, **extra) -> int:
    cid = companies.upsert(company)
    if size:
        db.update("companies", cid, {"size": size, "size_by": "model"})
    return db.insert("jobs", {"url": f"https://example.com/{title}-{company}".replace(" ", "-"), "title": title,
                              "company_name": company, "company_id": cid, "location": location, "work_mode": work_mode,
                              "fit_score": score, "status": "new", "parse_status": "parsed", "created_at": db.now(),
                              **extra})


def _rows():
    return db.q("SELECT * FROM jobs ORDER BY id")


def _points(job_id):
    return rank.annotate([db.q1("SELECT * FROM jobs WHERE id = ?", (job_id,))])[0]["rank"]["points"]


def test_distance_costs_a_quarter_point_a_km_from_home_up_to_the_limit():
    assert rank.distance_points(0, "onsite", 50) == 0
    assert rank.distance_points(21, "onsite", 50) == 5, "Skanderborg"
    assert rank.distance_points(33, "onsite", 50) == 8, "Fredericia"
    assert rank.distance_points(40, "onsite", 50) == 10, "Aarhus from Horsens"
    assert rank.distance_points(40, None, 50) == 10, "work mode not stated counts as on site"
    assert rank.distance_points(40, "hybrid", 50) == 10, "at the office much of the time, most of all at the start"
    assert rank.distance_points(40, "remote", 50) == 0
    assert rank.distance_points(48, "onsite", 50) == 12, "Kolding"
    assert rank.distance_points(70, "onsite", 50) == 12, "a job kept beyond the limit costs no more than one at it"
    assert rank.distance_points(None, "onsite", 50) == 0


def test_a_near_job_beats_a_better_fit_further_away_and_size_counts_only_when_you_turn_it_on(fake):
    _home(nudge="moderate")
    aarhus = _job("Linux Operations Engineer", "Brinkholm", "Aarhus", 72, size="very_large")
    horsens = _job("IT-supporter", "Lokal IT ApS", "Horsens", 66, size="small")
    vejle = _job("Developer", "Mellem A/S", "Vejle", 68, size="large")
    unscored = _job("Tester", "Lokal IT ApS", "Horsens", None)
    order = rank.best_first(_rows())
    assert [r["id"] for r in order] == [horsens, vejle, aarhus, unscored], "Vejle 68, 26 km, large employer is 58"
    a = next(r for r in order if r["id"] == aarhus)["rank"]
    assert a["score"] == 72 - 10 - 8 and a["points"] == [(10, "Aarhus, 40 km"), (8, "very large or very well-known employer")]
    assert next(r for r in order if r["id"] == vejle)["rank"]["points"] == [(6, "Vejle, 26 km"), (4, "large employer")]
    assert next(r for r in order if r["id"] == unscored)["rank"]["score"] is None

    settings.update("profile", {"big_employers": "strong"})
    assert rank.annotate([db.q1("SELECT * FROM jobs WHERE id = ?", (aarhus,))])[0]["rank"]["score"] == 72 - 10 - 16
    settings.update("profile", {"big_employers": ""})
    assert rank.annotate([db.q1("SELECT * FROM jobs WHERE id = ?", (aarhus,))])[0]["rank"]["score"] == 72 - 10, \
        "off unless you turn it on"


def test_distance_is_to_the_main_workplace_and_one_beyond_your_limit_is_set_aside(fake):
    _home()
    main = '{"main_location": "Aalborg"}'
    sap = _job("Junior SAP/ABAP-udvikler", "Nordvik", "Aalborg, Aarhus, Ballerup", 78, work_mode="hybrid", details=main)
    choose = _job("IT Consultant", "Brinkholm", "Copenhagen, Aarhus or Aalborg", 70)
    alone = _job("Developer", "Mellem A/S", "Aalborg", 70, details=main)
    ranked = {r["id"]: r["rank"] for r in rank.annotate(_rows())}
    assert ranked[sap]["points"] == [(12, "Aalborg, 132 km (main workplace) (hybrid)")] and ranked[sap]["score"] == 66
    assert ranked[choose]["points"] == [(10, "Aarhus, 40 km")], "offices you choose between: the nearest counts"
    assert ranked[alone]["points"] == [(12, "Aalborg, 132 km")], "one place: kept beyond the limit, no more than that"
    job = db.q1("SELECT * FROM jobs WHERE id = ?", (sap,))
    assert jobs.distance_line(job).endswith("The ad names Aalborg as the main workplace.")

    assert jobs.filter_too_far() == 2
    status = {r["id"]: (r["status"], r["notes"] or "") for r in _rows()}
    assert status[sap][0] == "filtered" and "Aalborg, 132 km from Horsens, the ad's main workplace" in status[sap][1]
    assert status[alone][0] == "filtered" and status[choose][0] == "new"


def test_a_place_the_app_cant_measure_costs_what_aarhus_does_and_abroad_what_the_limit_does(fake):
    _home()
    empty = _job("A", "Havbro", "", 70)
    region = _job("B", "Havbro", "Region Midtjylland", 70)
    abroad = _job("C", "Havbro", "Warsaw, Poland", 70, work_mode=None)
    remote = _job("D", "Havbro", "", 70, work_mode="remote")
    karup = _job("E", "Havbro", "Karup J", 70)
    assert _points(empty) == [(10, "location not stated")]
    assert _points(region) == [(10, "distance not known (Region Midtjylland)")]
    assert _points(abroad) == [(12, "outside Denmark")]
    assert _points(remote) == []
    assert _points(karup) == [(12, "Karup, 65 km")], "a town added to the list on 2026-11-28"


def test_the_kind_of_job_and_deal_breakers_count_only_on_fit_verdicts_made_without_them(fake):
    _home()
    new = '{{"basis": 2, "role_match": "{}", "dealbreakers": {}}}'
    target = _job("A", "Havbro", "Horsens", 70, fit=new.format("target", "[]"))
    related = _job("B", "Havbro", "Horsens", 70, fit=new.format("related", "[]"))
    outside = _job("C", "Havbro", "Horsens", 70, fit=new.format("outside", "[]"))
    breaker = _job("D", "Havbro", "Horsens", 70, fit=new.format("target", '["A temporary contract"]'))
    old = _job("E", "Havbro", "Horsens", 70, fit='{"role_match": "outside", "dealbreakers": ["50 km"]}')
    assert _points(target) == []
    assert _points(related) == [(7, "next to your target roles")]
    assert _points(outside) == [(30, "outside your target roles")]
    assert _points(breaker) == [(40, "deal-breaker: A temporary contract")]
    assert _points(old) == [], "an older verdict has these in its score already: nothing counts twice"
    low = _job("F", "Havbro", "Aarhus", 8, fit=new.format("outside", "[]"))
    assert rank.annotate([db.q1("SELECT * FROM jobs WHERE id = ?", (low,))])[0]["rank"]["score"] == 0, "never below 0"


def test_the_fit_prompt_never_sees_where_the_job_or_you_are(fake):
    db.insert("facts", {"category": "skill", "title": "Scrum", "created_at": db.now()})
    _home()
    settings.update("profile", {"locations": "Horsens, ideally within 25 km", "work_modes": ["onsite"]})
    job_id = _job("Developer", "Havbro", "Aarhus", None, description="Vi søger en udvikler.")
    assert jobs.score(job_id)["score"] == 86
    system, user, _ = next(c for c in fake.calls if c[2] is jobs.FIT_SCHEMA)
    for told in ("Distance from home", "Location:", "Aarhus", "Preferred locations", "Longest distance", "City:",
                 "Work modes"):
        assert told not in system + user, told
    assert "role_match" in jobs.FIT_SCHEMA["properties"]
    assert (db.jload(jobs.get(job_id)["fit"], {}) or {}).get("basis") == jobs.FIT_BASIS


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

    settings.update("profile", {"big_employers": "moderate"})
    page = ('<html><head><script type="application/ld+json">{"@type": "JobPosting", "title": "Product Owner", '
            '"validThrough": "2030-12-16", "description": "Product Owner hos FinPay. Søg snarest."}</script></head>'
            '<body><h1>Product Owner</h1><p>Product Owner hos FinPay. Søg snarest, vi holder samtaler løbende.</p>'
            '<p>' + 'Du får ansvaret for vores betalingsprodukter og arbejder tæt med udviklerne. ' * 10 + '</p>'
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
    assert jobs.same_title("Backendudvikler til Nordvik Sekoia", "Backend-udvikler til Nordvik Sekoia")
    assert jobs.same_title("AI-udvikler / AI Engineer", "AI-udvikler / AI Engineer til Krifa", "Krifa",
                           "Kristelig Fagbevægelse (KRIFA)")
    assert jobs.same_title("Platform Engineer", "Platform Engineer at Whiteaway Group", "WhiteAway Group", "Whiteaway Group")
    assert not jobs.same_title("IT Consultant", "IT Consultant, Brinkholm Consulting", "Brinkholm", "Brinkholm A/S"), \
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
    first = db.insert("jobs", {"url": "https://a.example/1", "title": "Linux Operations Engineer", "company_name": "Brinkholm",
                               "location": "Copenhagen, Aarhus", "source_id": a, "status": "new", "created_at": db.now()})
    second = db.insert("jobs", {"url": "https://b.example/1", "title": "Linux Operations Engineer: Copenhagen, Aarhus or Aalborg",
                                "company_name": "Brinkholm A/S", "location": "Aarhus", "source_id": b, "status": "new",
                                "created_at": db.now()})
    other = db.insert("jobs", {"url": "https://b.example/2", "title": "Microsoft Operations Engineer", "company_name": "Brinkholm A/S",
                               "location": "Aarhus", "source_id": b, "status": "new", "created_at": db.now()})
    assert jobs.mark_existing_repeats() == 1
    assert db.q1("SELECT duplicate_of FROM jobs WHERE id = ?", (second,))["duplicate_of"] == first
    assert db.q1("SELECT duplicate_of FROM jobs WHERE id = ?", (other,))["duplicate_of"] is None


def test_employer_sizes_are_judged_in_batches_and_yours_wins(fake):
    _home()
    big = _job("A", "Brinkholm", "Aarhus", 70)
    mid = _job("B", "Mellem A/S", "Vejle", 70)
    skipped = _job("C", "Ukendt Firma ApS", "Horsens", 70)
    mine = _job("D", "Min Virksomhed ApS", "Horsens", 70)
    _job("E", "Gammel ApS", "Horsens", 70, status="dismissed")
    companies.set_size(db.q1("SELECT company_id FROM jobs WHERE id = ?", (mine,))["company_id"], "large")
    assert [c["name"] for c in companies.unsized()] == ["Brinkholm", "Mellem A/S", "Ukendt Firma ApS"], \
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
    _job("Linux Operations Engineer", "Brinkholm", "Aarhus", 72, size="very_large", posted_at=date.today().isoformat())
    _job("IT-supporter", "Lokal IT ApS", "Horsens", 66, size="small", deadline="2030-01-01")
    c = TestClient(app)
    page = c.get("/jobs").text
    assert page.index("IT-supporter") < page.index("Linux Operations Engineer")
    assert "Fit 72: −10 Aarhus, 40 km</div>" in page
    assert 'title="Ranked 62 in your list: fit 72, −10 Aarhus, 40 km">62</span>' in page, "one number: the rank"
    assert ">72</span>" not in page and "<small>fit</small>" not in page, "the fit shows on hover, not as a badge"
    assert 'title="Ranked 66 in your list: fit 66, nothing taken off">66<' in page
    assert "rolling · apply soon" in page
    urgent = c.get("/jobs?sort=urgent").text
    assert urgent.index("Linux Operations Engineer") < urgent.index("IT-supporter")
    assert c.get("/jobs?sort=deadline").status_code == 200, "an old link to the deadline order still works"
    r = c.post("/profile/about", data={"city": "Horsens", "max_km": "50", "big_employers": "loud"},
               follow_redirects=False)
    assert r.status_code == 303 and settings.get("profile")["big_employers"] == "off"
    assert "preferred_km" not in c.get("/profile?tab=about").text, "distance counts from home: no free zone to set"
    c.post("/profile/about", data={"city": "Horsens", "max_km": "50", "big_employers": "moderate"}, follow_redirects=False)
    assert "Fit 72: −10 Aarhus, 40 km, −8 very large or very well-known employer" in c.get("/jobs").text
    cid = db.q1("SELECT id FROM companies WHERE name = 'Brinkholm'")["id"]
    c.post(f"/companies/{cid}", data={"size": "medium"}, follow_redirects=False)
    assert db.q1("SELECT size, size_by FROM companies WHERE id = ?", (cid,)) == {"size": "medium", "size_by": "you"}
    assert "Fit 72: −10 Aarhus, 40 km</div>" in c.get("/jobs").text
    c.post(f"/companies/{cid}", data={"size": ""}, follow_redirects=False)
    assert db.q1("SELECT size FROM companies WHERE id = ?", (cid,))["size"] is None
    assert db.scalar("SELECT COUNT(*) FROM tasks WHERE kind = 'size_companies'") >= 1, "handed back to the model"
    assert tasks.TASK_LABELS["size_companies"]


def test_every_job_shows_one_number_its_rank_with_the_fit_on_hover(fake):
    _home()
    main = '{"main_location": "Aalborg"}'
    sap = _job("Junior SAP/ABAP-udvikler", "Nordvik", "Aalborg, Aarhus, Ballerup", 78, work_mode="hybrid",
               size="very_large", details=main, deadline=(date.today() + timedelta(days=3)).isoformat())
    db.update("jobs", sap, {"fit": '{"score": 78, "verdict": "good", "summary": "A good fit."}'})
    app_id = db.insert("applications", {"job_id": sap, "status": "draft", "letter": "Kære Nordvik", "created_at": db.now()})
    badge = ('<span class="score score-good" title="Ranked 66 in your list: fit 78, −12 Aalborg, 132 km (main workplace) '
             '(hybrid)">66</span>')
    c = TestClient(app)
    today = c.get("/").text
    assert today.count(badge) == 2, "waiting for review, and the deadlines"
    cid = db.q1("SELECT company_id FROM jobs WHERE id = ?", (sap,))["company_id"]
    assert badge in c.get(f"/companies/{cid}").text
    assert badge in c.get(f"/applications/{app_id}").text
    job = c.get(f"/jobs/{sap}").text
    assert "Ranked 66 in your list: fit 78, −12 Aalborg, 132 km (main workplace) (hybrid)." in job
    assert badge.replace('score-good"', 'score-good big"') in job, "the fit card shows the rank too"
    assert ">78</span>" not in job, "no fit badge anywhere"


def test_an_ad_up_for_months_without_a_deadline_says_to_ask_first(fake):
    today = date(2026, 11, 22)
    year = rank.urgency({"title": "Udvikler", "posted_at": "2025-09-03"}, today)
    assert year["kind"] == "stale" and year["long_running"] and year["since"] == "2025-09-03"
    assert not rank.urgency({"title": "Udvikler", "posted_at": "2026-10-01"}, today)["long_running"], "under 3 months"
    assert not rank.urgency({"title": "Udvikler", "posted_at": "2025-09-03", "deadline": "2026-12-01"}, today)["long_running"], \
        "a deadline says when it closes"

    _home()
    old = _job("Udvikler til D365 FO", "Brinkholm ApS", "Horsens", 80, posted_at=(date.today() - timedelta(days=445)).isoformat(),
               contact_name="Mette Holm", contact_email="mh@brinkholm.example")
    newer = _job("Developer", "B ApS", "Horsens", 70, posted_at=(date.today() - timedelta(days=40)).isoformat())
    c = TestClient(app)
    listing = c.get("/jobs").text
    assert "· ask first" in listing and "open since" in listing
    page = c.get(f"/jobs/{old}").text
    assert "months without a deadline." in page and 'href="mailto:mh@brinkholm.example"' in page
    assert "months without a deadline." not in c.get(f"/jobs/{newer}").text
    app_id = jobs.create_application(old, draft=False)
    assert "months without a deadline." in c.get(f"/applications/{app_id}?tab=letter").text
    assert rank.annotate([db.q1("SELECT * FROM jobs WHERE id = ?", (old,))])[0]["rank"]["score"] == 80, \
        "the score is left alone: the age says nothing about the fit"
