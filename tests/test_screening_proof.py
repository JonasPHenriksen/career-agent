"""What you never see is decided by screening and the repeat check, so both are thorough and proven every week."""
from __future__ import annotations

from fastapi.testclient import TestClient

from career_agent import db, fetch, health, jobs, settings, sources
from career_agent.web.app import app

AD = "<html><body><h1>Product Owner</h1><p>" + "Vi søger en product owner til vores betalingsteam. " * 20 + "</p></body></html>"


def _facts():
    db.insert("facts", {"category": "skill", "title": "Scrum", "created_at": db.now()})


def _listing(n, title="Udvikler", company="Havbro", location="Horsens", text="Vi søger en udvikler."):
    return {"url": f"https://board.example/{n}", "title": title, "company": company, "location": location, "text": text}


def _answers(fake, first, second):
    """The two screenings' verdicts: first and second are sets of listing numbers each would keep."""
    import re

    base = fake.answer

    def answer(system, user, schema):
        if schema is sources.TRIAGE_SCHEMA:
            titles = re.findall(r"^\d+\. (.*?) —", user.split("LISTINGS:")[-1], re.M)
            keep = second if system == sources.SECOND_SYSTEM else first
            listed = [{"n": n, "keep": t in keep, "reason": f"{'second' if system == sources.SECOND_SYSTEM else 'first'}"
                       f" says {'keep' if t in keep else 'drop'} {t}"} for n, t in enumerate(titles)]
            return {"verdicts": [v for v in listed if "skip-me" not in titles[v["n"]]]}
        return base(system, user, schema)

    fake.answer = answer


def test_a_listing_is_set_aside_only_when_both_screenings_say_so(fake):
    _facts()
    _answers(fake, first={"Kept"}, second={"Rescued"})
    sid = sources.add("Feed", "https://board.example/feed", kind="rss")
    items = [_listing(1, "Kept"), _listing(2, "Rescued"), _listing(3, "Dropped"), _listing(4, "skip-me")]
    assert sources._screen_and_store(items, sid) == 3
    row = {r["title"]: r for r in db.q("SELECT * FROM jobs")}
    assert row["Kept"]["status"] == "new" and row["Rescued"]["status"] == "new", "the second opinion catches it"
    assert row["skip-me"]["status"] == "new", "a listing the model left out is kept, not dropped"
    assert row["Dropped"]["status"] == "filtered" and row["Dropped"]["notes"] == sources.FILTERED_NOTE
    assert (db.jload(row["Dropped"]["details"], {}))["screened_out"] == ["first says drop Dropped", "second says drop Dropped"]
    seconds = [u for s, u, schema in fake.calls if schema is sources.TRIAGE_SCHEMA and s == sources.SECOND_SYSTEM]
    assert len(seconds) == 1 and "Rescued" in seconds[0] and "Kept" not in seconds[0], "only the doubtful ones"


def test_the_rules_only_suggest_a_repeat_and_the_model_decides(fake):
    _facts()
    _answers(fake, first={"Udvikler"}, second=set())
    a, b = sources.add("A", "https://a.example/feed", kind="rss"), sources.add("B", "https://b.example/feed", kind="rss")
    sources._screen_and_store([_listing(1)], a)
    original = db.q1("SELECT * FROM jobs WHERE url = 'https://board.example/1'")

    fake.same_job = 0  # not certain: both stay
    sources._screen_and_store([_listing(2)], b)
    copy = db.q1("SELECT * FROM jobs WHERE url = 'https://board.example/2'")
    assert copy["duplicate_of"] is None and copy["status"] == "new"
    asked = [u for _, u, schema in fake.calls if schema is jobs.SAME_JOB_SCHEMA]
    assert len(asked) == 1 and "NEW AD" in asked[0] and "EARLIER ADS" in asked[0]

    fake.same_job = 1  # certain: the copy is hidden, with the model's reason
    sources._screen_and_store([_listing(3)], b)
    hidden = db.q1("SELECT * FROM jobs WHERE url = 'https://board.example/3'")
    assert hidden["duplicate_of"] == original["id"] and "judged them the same opening" in hidden["notes"]

    db.update("jobs", original["id"], {"status": "filtered", "notes": "Skipped: too far."})
    calls = len(fake.calls)
    assert jobs.confirm_repeat({"title": "Udvikler", "company_name": "Havbro", "text": "x"}, original) is None, \
        "a job the app set aside hides nothing: the copy is judged on its own"
    assert len(fake.calls) == calls
    db.update("jobs", original["id"], {"status": "dismissed"})
    assert jobs.confirm_repeat({"title": "Udvikler", "company_name": "Havbro", "text": "x"}, original), \
        "one you said is not for you still counts"


def test_looking_again_at_a_hidden_repeat_brings_another_job_back(fake):
    _facts()
    _answers(fake, first={"Udvikler"}, second=set())
    a, b = sources.add("A", "https://a.example/feed", kind="rss"), sources.add("B", "https://b.example/feed", kind="rss")
    sources._screen_and_store([_listing(1)], a)
    fake.same_job = 1
    sources._screen_and_store([_listing(2), _listing(3)], b)
    same, other = (db.q1("SELECT id FROM jobs WHERE url = ?", (f"https://board.example/{n}",))["id"] for n in (2, 3))
    assert jobs.recheck_repeat(same) == "same"
    fake.same_job = 0
    assert jobs.recheck_repeat(other) == "restored"
    back = db.q1("SELECT * FROM jobs WHERE id = ?", (other,))
    assert back["duplicate_of"] is None and back["status"] == "new", "screened like a new listing, and kept"


def test_the_weekly_proof_reads_screened_out_listings_and_brings_back_a_miss(fake, monkeypatch):
    _facts()
    settings.update("profile", {"city": "København", "max_km": "50"})

    class Page:
        text, url = AD, "https://board.example/x"

    monkeypatch.setattr(fetch, "ad_page", lambda url: Page())
    sid = sources.add("Feed", "https://board.example/feed", kind="rss")
    miss = jobs.add_from_url("https://board.example/miss", source_id=sid, title="Product Owner", queue=False)[0]
    db.update("jobs", miss, {"status": "filtered", "notes": sources.FILTERED_NOTE,
                             "details": '{"screened_out": ["another field", "agree"]}'})
    far = jobs.add_from_url("https://board.example/far", source_id=sid, title="Udvikler", queue=False)[0]
    db.update("jobs", far, {"status": "filtered", "notes": sources.FILTERED_NOTE, "location": "Aalborg"})
    run = health.proof()
    assert run["screened"] == 1 and run["misses"] == 1, "the fake model scores it 86; Aalborg is never read"
    assert jobs.get(far)["parse_status"] != "parsed"
    job = jobs.get(miss)
    assert job["status"] in ("new", "shortlisted") and "the weekly check read it" in job["notes"], "back in your list"
    last = health.proof_result()["screened"][0]
    assert last["outcome"] == "miss" and last["why"] == "another field; agree"
    assert "screening set aside 1 job worth reading this week" in health.summary()
    assert not health.proof_due()

    base = fake.answer
    fake.answer = lambda s, u, schema: ({**base(s, u, schema), "score": 30, "verdict": "poor"}
                                        if schema is jobs.FIT_SCHEMA else base(s, u, schema))
    right = jobs.add_from_url("https://board.example/right", source_id=sid, title="Product Owner 2", queue=False)[0]
    db.update("jobs", right, {"status": "filtered", "notes": sources.FILTERED_NOTE})
    db.update("jobs", miss, {"status": "dismissed"})
    run = health.proof()
    assert run == {**run, "screened": 1, "misses": 0}
    assert jobs.get(right)["status"] == "filtered", "rightly set aside: it goes back"
    totals = health.proof_totals()
    assert totals["screened"] == 2 and totals["misses"] == 1 and totals["runs"] == 2
    page = TestClient(app).get("/checks").text
    assert 'id="proof"' in page and "rightly set aside" in page and "screening set aside 1 worth reading of 2" in page


def test_two_live_copies_of_a_gone_ad_are_compared_with_each_other(fake):
    fake.same_job = 1
    first = jobs.add_from_text("Full stack developer hos Havbro i Kolding. " * 20)
    db.update("jobs", first, {"title": "Full stack developer", "company_name": "Havbro", "parse_status": "parsed",
                              "gone_at": db.now(), "description": "Full stack developer hos Havbro i Kolding. " * 20})
    live = jobs.add_from_text("Full stack developer hos Havbro i Kolding. " * 20)
    db.update("jobs", live, {"title": "Full stack developer", "company_name": "Havbro", "parse_status": "parsed",
                             "description": "Full stack developer hos Havbro i Kolding. " * 20})
    later = jobs.add_from_text("x")
    text = "Full stack developer hos Havbro i Kolding. " * 20
    assert jobs.find_duplicate(later, "Full stack developer", text)["id"] == live, "past the gone ad, to the live copy"
