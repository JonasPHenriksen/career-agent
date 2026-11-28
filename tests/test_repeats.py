"""The same job from several places: one copy stays, the others are set aside as repeats of it."""
from __future__ import annotations

import pytest

from career_agent import db, jobs, sources

from .conftest import JOB_TEXT


def _source(name: str) -> int:
    return db.insert("sources", {"name": name, "kind": "rss", "url": f"https://{name}.example/feed", "created_at": db.now()})


def _item(url, title="Product Owner", company="FinPay A/S", location="", readable=True):
    it = {"url": url, "title": title, "company": company, "location": location, "text": ""}
    if not readable:
        it["readable"] = False
    return it


def _job(url):
    return db.q1("SELECT * FROM jobs WHERE url = ?", (url,))


@pytest.fixture(autouse=True)
def certain_copies(fake):
    """The model confirms the copies the rules suggest (jobs.confirm_repeat); a test of a "no" says so."""
    fake.same_job = 1



def test_keys_and_places():
    assert jobs.same_key("FinPay A/S", "Product Owner (m/k)") == jobs.same_key("Finpay", "product owner")
    assert jobs.same_key("", "Product Owner") is None and jobs.same_key("Ukendt", "PO") is None
    assert jobs.same_place("København K", "København") and jobs.same_place("", "Aarhus")
    assert jobs.same_place("Vejle, Denmark (hybrid)", "Vejle")
    assert not jobs.same_place("Aarhus", "Vejle")


def test_one_company_written_differently_on_different_sites_is_one_company():
    same = [("VELUX Group", "VELUX A/S"), ("Vattenfall A/S (via Academic Work Denmark A/S)", "VATTENFALL A/S"),
            ("Region Syddanmark – Regional IT", "Region Syddanmark"), ("Twoday Denmark", "Twoday A/S"),
            ("AAK - Aarhuskarlshamn", "AAK Denmark A/S"), ("Arla Foods amba", "Arla Foods"),
            ("Aarhus Universitet (AU)", "Aarhus Universitet, AU HR - Data og Digitalisering"),
            ("SCR ApS  søger for Bizbrains", "Bizbrains ApS"), ("Bloom recruiting on behalf of Whiteaway Group", "WhiteAway")]
    for a, b in same:
        assert jobs.same_key(a, "Platform Engineer") == jobs.same_key(b, "Platform Engineer"), (a, b)
    assert jobs.same_key("Lunar ApS", "Platform Engineer") != jobs.same_key("Mind Energy A/S", "Platform Engineer")
    assert jobs.same_key("Hewlett-Packard", "x") == "hewlett packard|x" and jobs.company_core("Group") == "group"
    for a, b in [("Energinet", "Energinet Eltransmission A/S"), ("Knowit Solutions Danmark A/S", "Knowit"),
                 ("Arla", "Arla Foods amba"), ("CyberPilot ApS", "CyberPilot.io"), ("Danish Agro a.m.b.a.", "Danish Agro")]:
        assert jobs.same_company(a, b) and jobs.same_name(a, "IT-supporter (m/k)", b, "IT-supporter"), (a, b)
    assert not jobs.same_company("NRGi", "Nordenta") and not jobs.same_company("Ukendt", "Ukendt")
    assert not jobs.same_name("Energinet", "OT Security Engineer", "Energinet", "OT Architect")


def test_the_sweep_keeps_one_copy_per_company_when_several_companies_share_a_title(fake):
    a, b = _source("jobindex"), _source("jobnet")
    sources._screen_and_store([_item("https://a.example/1", title="IT-supporter", company="NRGi", location="Aarhus"),
                               _item("https://a.example/2", title="IT-supporter", company="Nordenta", location="Hørning")], a)
    db.ex("UPDATE jobs SET company_name = 'Energinet' WHERE url = 'https://a.example/1'")
    db.insert("jobs", {"url": "https://b.example/9", "title": "IT-supporter", "company_name": "Energinet Eltransmission A/S",
                       "location": "Aarhus N", "source_id": b, "status": "new", "created_at": db.now()})
    assert jobs.mark_existing_repeats() == 1
    assert _job("https://b.example/9")["duplicate_of"] == _job("https://a.example/1")["id"]
    assert _job("https://a.example/2")["duplicate_of"] is None


def test_a_job_already_here_from_another_place_is_set_aside_before_screening(fake):
    a, b = _source("jobindex"), _source("jobbank")
    sources._screen_and_store([_item("https://jobindex.example/1", location="København K")], a)
    first = _job("https://jobindex.example/1")
    assert first["status"] == "new" and first["duplicate_of"] is None

    calls = len(fake.calls)
    sources._screen_and_store([_item("https://jobbank.example/9", title="Product owner", company="FinPay",
                                     location="København"),
                               _item("https://jobbank.example/10", title="Product Owner", location="Aarhus")], b)
    repeat = _job("https://jobbank.example/9")
    assert repeat["status"] == "filtered" and repeat["duplicate_of"] == first["id"]
    assert repeat["parse_status"] == "skipped", "a repeat is never read"
    assert f"job {first['id']} (jobindex)" in repeat["notes"]
    assert _job("https://jobbank.example/10")["duplicate_of"] is None, "same title in another city: another job"
    assert [c[2] for c in fake.calls[calls:]] == [jobs.SAME_JOB_SCHEMA], \
        "the model only confirmed the repeat: it was neither screened nor read"
    assert [r["id"] for r in jobs.repeats_of(first["id"])] == [repeat["id"]]


def test_one_site_with_two_ads_of_the_same_title_keeps_both(fake):
    s = _source("page")
    sources._screen_and_store([_item("https://x.example/1"), _item("https://x.example/2")], s)
    assert _job("https://x.example/2")["duplicate_of"] is None
    sources._screen_and_store([_item("https://x.example/3")], s)
    assert _job("https://x.example/3")["duplicate_of"] is None


def test_the_same_job_twice_behind_links_the_app_may_not_open(fake):
    mail = _source("collected")
    sources._screen_and_store([_item("https://track.example/a", readable=False),
                               _item("https://track.example/b", readable=False)], mail)
    one, two = _job("https://track.example/a"), _job("https://track.example/b")
    assert one["duplicate_of"] is None and two["duplicate_of"] == one["id"]
    sources._screen_and_store([_item("https://track.example/c", readable=False)], mail)
    assert _job("https://track.example/c")["duplicate_of"] == one["id"]


def test_a_job_you_said_is_not_for_you_hides_new_copies(fake):
    a, b = _source("jobindex"), _source("jobbank")
    sources._screen_and_store([_item("https://jobindex.example/1")], a)
    first = _job("https://jobindex.example/1")
    jobs.set_status(first["id"], "dismissed")
    sources._screen_and_store([_item("https://jobbank.example/2")], b)
    copy = _job("https://jobbank.example/2")
    assert copy["duplicate_of"] == first["id"] and copy["status"] == "filtered" and "is not for you" in copy["notes"]
    # ... for three months, not just the three weeks a repeat counts for
    db.ex("UPDATE jobs SET created_at = ? WHERE id = ?", (db.days_ago(60), first["id"]))
    sources._screen_and_store([_item("https://jobbank.example/3")], b)
    assert _job("https://jobbank.example/3")["duplicate_of"] == first["id"]
    db.ex("UPDATE jobs SET created_at = ? WHERE id = ?", (db.days_ago(100), first["id"]))
    sources._screen_and_store([_item("https://jobbank.example/4")], b)
    assert _job("https://jobbank.example/4")["duplicate_of"] is None


def test_a_dismissed_locked_copy_hides_a_readable_one(fake):
    mail, feed = _source("collected"), _source("feed")
    sources._screen_and_store([_item("https://track.example/c?x=1", readable=False)], mail)
    jobs.set_status(_job("https://track.example/c?x=1")["id"], "dismissed")
    sources._screen_and_store([_item("https://feed.example/7")], feed)
    assert _job("https://feed.example/7")["duplicate_of"] == _job("https://track.example/c?x=1")["id"]


def test_not_for_me_sets_aside_the_copies_already_in_your_inbox(fake):
    a, b, c = _source("jobindex"), _source("jobbank"), _source("jobnet")
    sources._screen_and_store([_item("https://jobindex.example/1", location="Aarhus")], a)
    first = _job("https://jobindex.example/1")
    # a copy from more than three weeks later is its own job until you say the first is not for you
    db.insert("jobs", {"url": "https://jobbank.example/2", "title": "Product Owner", "company_name": "FinPay",
                       "location": "Aarhus C", "source_id": b, "status": "new", "created_at": db.now()})
    db.ex("UPDATE jobs SET created_at = ? WHERE id = ?", (db.days_ago(40), first["id"]))
    shortlisted = db.insert("jobs", {"url": "https://jobnet.example/3", "title": "Product Owner", "company_name": "FinPay",
                                     "location": "Aarhus", "source_id": c, "status": "shortlisted", "created_at": db.now()})
    elsewhere = db.insert("jobs", {"url": "https://jobnet.example/4", "title": "Product Owner", "company_name": "FinPay",
                                   "location": "Vejle", "source_id": c, "status": "new", "created_at": db.now()})
    gone = jobs.dismiss(first["id"])
    assert gone["copies"] == 1 and not gone["draft_removed"]
    assert _job("https://jobbank.example/2")["duplicate_of"] == first["id"]
    assert jobs.get(shortlisted)["duplicate_of"] is None, "a job you shortlisted is yours to decide"
    assert jobs.get(elsewhere)["duplicate_of"] is None, "same title in another town is another job"


def test_not_for_me_removes_an_unsent_draft_but_never_a_sent_application(fake):
    one = db.insert("jobs", {"url": "https://x.example/1", "title": "PO", "company_name": "A", "status": "shortlisted",
                             "created_at": db.now()})
    two = db.insert("jobs", {"url": "https://x.example/2", "title": "PM", "company_name": "B", "status": "shortlisted",
                             "created_at": db.now()})
    draft = db.insert("applications", {"job_id": one, "status": "draft", "created_at": db.now(), "updated_at": db.now()})
    from career_agent import tasks
    tasks.enqueue("draft_application", {"application_id": draft}, ref=f"app:{draft}")
    sent = db.insert("applications", {"job_id": two, "status": "applied", "created_at": db.now(), "updated_at": db.now()})
    assert jobs.dismiss(one)["draft_removed"] and not db.q1("SELECT 1 FROM applications WHERE id = ?", (draft,))
    assert not db.q1("SELECT 1 FROM tasks WHERE ref = ? AND status = 'queued'", (f"app:{draft}",))
    assert not jobs.dismiss(two)["draft_removed"] and db.q1("SELECT 1 FROM applications WHERE id = ?", (sent,))
    assert jobs.get(one)["status"] == jobs.get(two)["status"] == "dismissed"


def test_a_locked_copy_never_stands_in_for_one_the_app_can_read(fake):
    mail, feed = _source("collected"), _source("feed")
    sources._screen_and_store([_item("https://track.example/c?x=1", readable=False)], mail)
    locked = _job("https://track.example/c?x=1")
    assert locked["link_locked"] == 1 and locked["parse_status"] == "skipped" and locked["status"] == "new"

    sources._screen_and_store([_item("https://feed.example/7")], feed)
    readable = _job("https://feed.example/7")
    assert readable["duplicate_of"] is None and readable["parse_status"] == "pending"
    locked = _job("https://track.example/c?x=1")
    assert locked["status"] == "filtered" and locked["duplicate_of"] == readable["id"]

    # And a later locked copy of a job that's already here is only a repeat.
    sources._screen_and_store([_item("https://track.example/c?x=2", readable=False)], mail)
    assert _job("https://track.example/c?x=2")["duplicate_of"] == readable["id"]


def test_a_repeat_found_when_the_ad_is_read(fake):
    a, b = _source("jobindex"), _source("company-page")
    sources._screen_and_store([_item("https://jobindex.example/1", location="København")], a)
    first = _job("https://jobindex.example/1")
    db.update("jobs", first["id"], {"parse_status": "parsed"})
    # The company's own page names no company in its listing, so it gets through screening and is read.
    sources._screen_and_store([_item("https://finpay.example/jobs/po", company="")], b)
    second = _job("https://finpay.example/jobs/po")
    db.update("jobs", second["id"], {"description": JOB_TEXT})
    assert jobs.parse(second["id"], use_text=True)["skipped"] == "duplicate"
    second = _job("https://finpay.example/jobs/po")
    assert second["status"] == "filtered" and second["duplicate_of"] == first["id"]
    assert not db.q1("SELECT 1 FROM tasks WHERE kind = 'score_job' AND ref = ?", (f"job:{second['id']}",)), \
        "a repeat isn't scored"


def test_keep_it_as_its_own_job(fake):
    from fastapi.testclient import TestClient

    from career_agent.web.app import app

    a, b = _source("jobindex"), _source("jobbank")
    sources._screen_and_store([_item("https://jobindex.example/1")], a)
    sources._screen_and_store([_item("https://jobbank.example/2")], b)
    repeat = _job("https://jobbank.example/2")
    assert repeat["duplicate_of"]
    client = TestClient(app)
    page = client.get(f"/jobs/{repeat['duplicate_of']}")
    assert "Also posted at" in page.text and "jobbank" in page.text
    assert "already in your list" in client.get(f"/jobs/{repeat['id']}").text
    client.post(f"/jobs/{repeat['id']}/reparse", follow_redirects=False)
    kept = _job("https://jobbank.example/2")
    assert kept["keep"] == 1 and kept["duplicate_of"] is None and kept["status"] == "new"
    db.update("jobs", kept["id"], {"description": JOB_TEXT})
    assert "skipped" not in jobs.parse(kept["id"], use_text=True), "a job you kept is never set aside again"
    assert _job("https://jobbank.example/2")["status"] == "new"
    assert not sources.rescreen()["screened"] or _job("https://jobbank.example/2")["status"] == "new"


def test_cleaning_up_repeats_already_in_the_list(fake):
    a, b, c = _source("jobindex"), _source("jobbank"), _source("company")

    def add(url, src, **kw):
        row = {"url": url, "source_id": src, "title": "Full stack developer", "company_name": "Nic. Christiansen Gruppen",
               "status": "new", "parse_status": "parsed", "location": "Kolding", "created_at": db.now()}
        jid = db.insert("jobs", {**row, **kw})
        return jid

    first = add("https://jobindex.example/1", a, fit_score=85)
    second = add("https://jobbank.example/2", b, fit_score=82)
    liked = add("https://company.example/3", c, status="shortlisted")
    elsewhere = add("https://jobbank.example/4", b, location="Aalborg")
    add("https://jobindex.example/5", a, title="Backend developer", description="Payments team. " * 40)
    same_site = add("https://jobindex.example/6", a, title="Backend developer", description="Data platform. " * 40)
    assert jobs.mark_existing_repeats() == 2
    assert _job("https://company.example/3")["duplicate_of"] is None, "the one you shortlisted stays"
    assert db.q1("SELECT duplicate_of FROM jobs WHERE id = ?", (first,))["duplicate_of"] == liked
    assert db.q1("SELECT duplicate_of FROM jobs WHERE id = ?", (second,))["duplicate_of"] == liked
    assert db.q1("SELECT duplicate_of FROM jobs WHERE id = ?", (elsewhere,))["duplicate_of"] is None
    assert db.q1("SELECT duplicate_of FROM jobs WHERE id = ?", (same_site,))["duplicate_of"] is None, \
        "a second ad with the same title on the same site, with another text, is another job"
    assert jobs.mark_existing_repeats() == 0


def test_a_job_is_never_set_aside_as_a_repeat_of_itself(fake):
    from career_agent import settings
    a = _source("jobindex")
    sources._screen_and_store([_item("https://jobindex.example/1")], a)
    job = _job("https://jobindex.example/1")
    jobs.mark_repeat(job["id"], job)
    assert _job("https://jobindex.example/1")["duplicate_of"] is None
    # one already stored that way is put right once, after the update
    db.ex("UPDATE jobs SET duplicate_of = id, status = 'filtered' WHERE id = ?", (job["id"],))
    settings.update("state", {"jobs_tidied": 1})
    assert jobs.tidy_after_update()["self_repeats"] == 1
    fixed = _job("https://jobindex.example/1")
    assert fixed["duplicate_of"] is None and "repeat of itself" in fixed["notes"]
