"""Is the ad still up? Checked before writing, regularly, and when you open the job; a gone ad is shown as gone."""
from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from career_agent import adcheck, db, fetch, jobs, rank, settings, tasks, writer
from career_agent.adcheck import page_state as real_page_state
from career_agent.web.app import app

AD = ("<html><body><h1>Associate Software Engineer</h1><p>Vi søger en Associate Software Engineer til vores team. "
      "Vi lukker opslaget, når stillingen er besat. " + "Du får gode kolleger. " * 200 + "</p></body></html>")


class Resp:
    def __init__(self, url, text, ctype="text/html"):
        self.url, self.text, self.headers, self.status_code = url, text, {"content-type": ctype}, 200


@pytest.fixture()
def pages(monkeypatch):
    """Pretend sites: {url: (final url, html)}, or {url: FetchError}."""
    site: dict = {}

    def get(url, timeout=25):
        hit = site[url]
        if isinstance(hit, Exception):
            raise hit
        return Resp(*hit)

    monkeypatch.setattr(fetch, "get", get)
    return site


def test_what_a_gone_ad_looks_like(pages):
    title = "Associate Software Engineer"
    pages["https://jobbank.dk/job/1234567/x"] = fetch.FetchError("Page not found.", gone=True)
    assert real_page_state("https://jobbank.dk/job/1234567/x", title)[0] == "gone"
    pages["https://a.example/job/1"] = fetch.FetchError("The site took too long to answer.", connection=True)
    assert real_page_state("https://a.example/job/1", title)[0] == "unknown", "no answer is not gone"

    workday = "https://nordvik.wd3.myworkdayjobs.com/Nordvik_External/job/Vejle/Associate_0000012345"
    pages[workday] = (workday, "<html><body>Loading…</body></html>")
    assert real_page_state(workday, title) == ("gone", "the ad is no longer on nordvik.wd3.myworkdayjobs.com")
    ld = '<script type="application/ld+json">{"@type": "JobPosting", "title": "x", "validThrough": "%s"}</script>'
    pages[workday] = (workday, ld % "2099-01-01")
    assert real_page_state(workday, title)[0] == "open"
    pages[workday] = (workday, ld % "2020-01-01")
    assert real_page_state(workday, title) == ("gone", "nordvik.wd3.myworkdayjobs.com says the listing ended 2020-01-01")
    teamtailor = "https://careers.nordvik.example/jobs/8541036-associate-software-engineer"
    closed = (ld.replace(', "validThrough": "%s"', '') + "<h1>Associate Software Engineer</h1>"
              "<h2>Dette jobopslag er ikke længere aktivt</h2><p>Enten er jobbet besat, eller også er opslaget udløbet.</p>")
    pages[teamtailor] = (teamtailor, closed)
    assert real_page_state(teamtailor, title) == \
        ("gone", 'careers.nordvik.example says "jobopslag er ikke længere aktivt"'), "the ad's data stays on a closed ad"

    pages["https://www.jobindex.dk/vis-job/r1234567"] = ("https://www.jobindex.dk/jobsoegning", "<html>Jobs</html>")
    assert real_page_state("https://www.jobindex.dk/vis-job/r1234567", title)[0] == "gone", "sent on to the job list"
    pages["https://www.jobindex.dk/c?t=h7654321"] = ("https://careers.example.com/ad/5", AD)
    assert real_page_state("https://www.jobindex.dk/c?t=h7654321", title)[0] == "open", "a board sends you on to the employer"

    pages["https://b.example/job/2"] = ("https://b.example/job/2", "<html><h1>Opslag ikke fundet</h1><p>Prøv at søge.</p></html>")
    assert real_page_state("https://b.example/job/2", title) == ("gone", 'b.example says "Opslag ikke fundet"')
    pages["https://b.example/job/3"] = ("https://b.example/job/3", AD)
    assert real_page_state("https://b.example/job/3", title)[0] == "open", "an ad that says it closes when filled is up"
    expired = ("<html><h1>AI & Automation Specialist til Havbro A/S</h1><p>Annoncen er udløbet! Klik på annoncen for at se, "
               "om virksomheden stadig er åben for ansøgninger.</p>" + "<p>Lignende job i nærheden.</p>" * 200 + "</html>")
    pages["https://www.jobindex.dk/vis-job/h7654000"] = ("https://www.jobindex.dk/vis-job/h7654000", expired)
    assert real_page_state("https://www.jobindex.dk/vis-job/h7654000", "AI & Automation Specialist") == \
        ("gone", 'jobindex.dk says "Annoncen er udløbet"'), "a long page with the title still says it's closed"
    later = AD.replace("Vi lukker opslaget", "Når ansøgningsfristen er udløbet, holder vi samtaler. Vi lukker opslaget")
    pages["https://b.example/job/5"] = ("https://b.example/job/5", later)
    assert real_page_state("https://b.example/job/5", title)[0] == "open", "the ad's own words about its deadline"
    pages["https://b.example/job/4"] = ("https://b.example/job/4", "<html><div id='app'></div></html>")
    assert real_page_state("https://b.example/job/4", title)[0] == "unknown", "built in the browser: can't tell"


def _job(url="https://jobbank.dk/job/1/x", **extra) -> int:
    return db.insert("jobs", {"url": url, "title": "Associate Software Engineer", "company_name": "Nordvik A/S",
                              "status": "new", "parse_status": "parsed", "fit_score": 80, "created_at": db.now(), **extra})


def test_a_job_is_gone_only_when_no_copy_is_up(fake, monkeypatch):
    states = {}
    monkeypatch.setattr(adcheck, "page_state", lambda url, title: states[url])
    job = _job()
    copy = _job("https://nordvik.example/job/1", duplicate_of=job, status="filtered")
    states.update({"https://jobbank.dk/job/1/x": ("gone", "jobbank.dk says the page doesn't exist"),
                   "https://nordvik.example/job/1": ("open", "up on nordvik.example")})
    assert adcheck.check_job(job) == {"state": "open", "note": "up on nordvik.example (gone from jobbank.dk)"}
    assert db.q1("SELECT gone_at FROM jobs WHERE id = ?", (job,))["gone_at"] is None
    states["https://nordvik.example/job/1"] = ("unknown", "blocked")
    assert adcheck.check_job(job)["state"] == "gone", "its own page is gone and the copy says nothing"
    assert adcheck.check_job(copy)["state"] == "gone", "a copy is checked with its original"
    first = db.q1("SELECT gone_at FROM jobs WHERE id = ?", (job,))["gone_at"]
    assert first and jobs.is_expired(jobs.get(job))
    states["https://jobbank.dk/job/1/x"] = ("unknown", "no answer")
    adcheck.check_job(job)
    assert db.q1("SELECT gone_at FROM jobs WHERE id = ?", (job,))["gone_at"] == first, "a page that can't be read changes nothing"


def test_an_expired_board_copy_is_not_gone_while_the_page_you_apply_on_is_up(fake, monkeypatch):
    board, form = "https://www.jobindex.dk/vis-job/h1234567", "https://app.jobmatch.example/abc?client=jobindex"
    states = {board: ("gone", 'jobindex.dk says "Annoncen er udløbet"'), form: ("open", "up on app.jobmatch.example")}
    asked = []
    monkeypatch.setattr(adcheck, "page_state", lambda url, title: asked.append(url) or states[url])
    job = _job(board, apply_url=form, status="shortlisted")
    assert adcheck.check_job(job) == {"state": "open", "note": "up on app.jobmatch.example (gone from jobindex.dk)"}
    assert db.q1("SELECT gone_at FROM jobs WHERE id = ?", (job,))["gone_at"] is None
    states[form] = ("unknown", "couldn't tell")
    assert adcheck.check_job(job)["state"] == "gone", "an apply page that says nothing doesn't keep it open"
    states[board] = ("open", "up on jobindex.dk")
    asked.clear()
    assert adcheck.check_job(job)["state"] == "open" and asked == [board], "the apply page is only looked at when needed"


def test_writing_stops_before_any_model_call_when_the_ad_is_gone(fake, monkeypatch):
    monkeypatch.setattr(adcheck, "page_state", lambda url, title: ("gone", "jobbank.dk says the page doesn't exist"))
    db.insert("facts", {"category": "skill", "title": "Python", "created_at": db.now()})
    job = _job()
    app_id = jobs.create_application(job, draft=False)
    calls = len(fake.calls)
    assert writer.draft(app_id) == {"skipped": "the ad is gone"}
    assert len(fake.calls) == calls and db.q1("SELECT status, letter FROM applications WHERE id = ?", (app_id,)) == \
        {"status": "draft", "letter": None}

    monkeypatch.setattr(adcheck, "page_state", lambda url, title: ("open", "up on jobbank.dk"))
    db.update("jobs", job, {"checked_at": db.now()})
    assert adcheck.still_up(job), "found gone earlier: looked at again before writing, and it's back"


def test_auto_drafting_skips_a_gone_ad_and_takes_the_next(fake, monkeypatch):
    settings.update("automation", {"auto_draft": True, "auto_draft_daily_cap": 1})
    gone, up = _job("https://a.example/job/1", fit_score=90), _job("https://b.example/job/2", fit_score=85)
    monkeypatch.setattr(adcheck, "page_state", lambda url, title: ("gone", "x") if "a.example" in url else ("open", "y"))
    assert jobs.draft_best() == [up]
    assert db.q1("SELECT gone_at FROM jobs WHERE id = ?", (gone,))["gone_at"]


def test_a_limit_of_0_writes_no_drafts(fake, monkeypatch):
    settings.update("automation", {"auto_draft": True, "auto_draft_daily_cap": 0})
    _job("https://b.example/job/2", fit_score=95)
    monkeypatch.setattr(adcheck, "page_state", lambda url, title: ("open", "y"))
    assert jobs.draft_best() == [] and not db.q1("SELECT 1 FROM applications"), "a limit of 0 is 0, not the default 5"
    settings.update("automation", {"auto_draft_daily_cap": 1})
    assert len(jobs.draft_best()) == 1


def test_the_regular_check_looks_at_what_you_might_spend_time_on(fake):
    settings.update("profile", {"city": "Horsens"})
    drafted = _job("https://a.example/1")
    jobs.create_application(drafted, draft=False)
    sent = _job("https://a.example/2")
    db.update("applications", jobs.create_application(sent, draft=False), {"status": "applied"})
    shortlisted = _job("https://a.example/3", status="shortlisted")
    top = _job("https://a.example/4")
    poor = _job("https://a.example/5", fit_score=30)
    fresh = _job("https://a.example/6", checked_at=db.now())
    gone_recently = _job("https://a.example/7", status="shortlisted", gone_at=db.now(), checked_at=db.now())
    assert set(adcheck.to_check()) == {drafted, shortlisted, top}
    assert poor and fresh and gone_recently and sent
    db.update("jobs", drafted, {"gone_at": db.now(), "checked_at": db.days_ago(0.5)})
    assert drafted not in adcheck.to_check(), "a gone ad with a draft is looked at again once a day"
    db.update("jobs", drafted, {"checked_at": db.days_ago(1.1)})
    assert drafted in adcheck.to_check()

    assert adcheck.due()
    result = adcheck.check_many()
    assert result["checked"] == 3 and not adcheck.due(), "the next regular look is in a few hours"


def test_a_gone_ad_leaves_the_inbox_and_shows_where_you_work(fake):
    settings.update("profile", {"city": "Horsens"})
    job = _job(gone_at=db.now(), checked_at=db.now(), check_note="jobbank.dk says the page doesn't exist")
    other = _job("https://a.example/9", posted_at=date.today().isoformat())
    app_id = jobs.create_application(job, draft=False)
    jobs.create_application(other, draft=False)
    db.ex("UPDATE applications SET letter = 'Kære Nordvik', status = 'draft'")
    c = TestClient(app)
    assert "ad gone" in c.get("/").text
    order = [r["job_id"] for r in rank.urgent_first(db.q(
        "SELECT a.*, j.title AS job_title, j.fit_score, j.deadline, j.details, j.posted_at, j.created_at AS job_created_at, "
        "j.gone_at, j.open_said_at FROM applications a JOIN jobs j ON j.id = a.job_id"))]
    assert order == [other, job], "an application whose ad is gone goes last"
    page = c.get(f"/applications/{app_id}?tab=letter").text
    assert "This job ad is gone." in page and "jobbank.dk says the page doesn&#39;t exist" in page
    assert job not in [r["id"] for r in db.q(f"SELECT id FROM jobs j WHERE {jobs.open_sql()}", (date.today().isoformat(),))]

    c.post(f"/jobs/{job}/still-open", follow_redirects=False)
    assert "This job ad is gone." not in c.get(f"/applications/{app_id}?tab=letter").text
    adcheck.page_state = lambda url, title: ("gone", "still gone")  # the autouse fixture puts it back
    adcheck.check_job(job)
    assert db.q1("SELECT gone_at FROM jobs WHERE id = ?", (job,))["gone_at"] is None, "your word stands"

    db.ex("DELETE FROM tasks")
    c.post(f"/jobs/{other}/check", follow_redirects=False)
    assert db.q1("SELECT payload FROM tasks WHERE kind = 'check_ads'")["payload"] == f'{{"job_ids": [{other}]}}'
    assert tasks.TASK_LABELS["check_ads"]


def test_opening_a_job_looks_at_its_ad_when_it_has_not_been_for_a_while(fake):
    job = _job()
    TestClient(app).get(f"/jobs/{job}")
    assert db.scalar("SELECT COUNT(*) FROM tasks WHERE kind = 'check_ads'") == 1
    db.ex("DELETE FROM tasks")
    db.update("jobs", job, {"checked_at": db.now()})
    TestClient(app).get(f"/jobs/{job}")
    assert db.scalar("SELECT COUNT(*) FROM tasks WHERE kind = 'check_ads'") == 0


def test_a_gone_job_with_nothing_sent_can_be_removed_and_brought_back(fake):
    job = _job(gone_at=db.now(), checked_at=db.now(), check_note="jobbank.dk says the page doesn't exist")
    copy = _job("https://nordvik.example/job/1", duplicate_of=job, status="filtered")
    app_id = jobs.create_application(job, draft=False)
    db.update("applications", app_id, {"letter": "Kære Nordvik", "status": "draft"})
    tasks.enqueue("draft_application", {"application_id": app_id}, ref=f"app:{app_id}")
    c = TestClient(app)
    page = c.get(f"/applications/{app_id}?tab=letter").text
    assert "Remove job and draft" in page and 'name="next" value="/"' in page

    r = c.post(f"/jobs/{job}/remove-gone", data={"next": "/"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/?")
    assert not db.q("SELECT id FROM jobs WHERE id IN (?, ?)", (job, copy))
    assert not db.q1("SELECT id FROM applications WHERE id = ?", (app_id,))
    assert not db.q1("SELECT id FROM tasks WHERE ref = ?", (f"app:{app_id}",)), "no draft is written for it later"

    from career_agent import history
    op = int(r.headers["location"].split("undo=")[1].split("&")[0].split("#")[0])
    history.undo(op)
    assert db.q1("SELECT id FROM applications WHERE id = ?", (app_id,)), "it can be brought back"


def test_only_a_gone_job_with_nothing_sent_is_removed_this_way(fake):
    up = _job("https://a.example/1")
    sent = _job("https://a.example/2", gone_at=db.now(), checked_at=db.now(), check_note="gone")
    db.update("applications", jobs.create_application(sent, draft=False), {"status": "applied"})
    c = TestClient(app)
    for job in (up, sent):
        c.post(f"/jobs/{job}/remove-gone", follow_redirects=False)
        assert db.q1("SELECT id FROM jobs WHERE id = ?", (job,)), job
    assert "Remove job" not in c.get(f"/jobs/{sent}").text


def test_auto_drafting_leaves_an_ad_up_for_months_for_you_to_ask_about_first(fake, monkeypatch):
    from datetime import timedelta

    settings.update("automation", {"auto_draft": True, "auto_draft_daily_cap": 2})
    monkeypatch.setattr(adcheck, "page_state", lambda url, title: ("open", "up"))
    old = _job("https://a.example/job/1", fit_score=95, posted_at=(date.today() - timedelta(days=200)).isoformat())
    dated = _job("https://a.example/job/2", fit_score=90, posted_at=(date.today() - timedelta(days=200)).isoformat(),
                 deadline=(date.today() + timedelta(days=10)).isoformat())
    fresh = _job("https://a.example/job/3", fit_score=85, posted_at=date.today().isoformat())
    assert jobs.draft_best() == [dated, fresh], "an old ad with a deadline says when it closes, so it's drafted"
    assert not db.q1("SELECT id FROM applications WHERE job_id = ?", (old,))
    app_id = jobs.create_application(old)  # pressing Write still drafts it
    assert db.q1("SELECT kind FROM tasks WHERE ref = ?", (f"app:{app_id}",))["kind"] == "draft_application"
