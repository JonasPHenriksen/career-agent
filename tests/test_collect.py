"""Jobs collected from a results page you have open: the "Collect jobs" bookmark, its page and what it stores."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from career_agent import collect, db, robots, sources, tasks
from career_agent.web.app import app

# The shape of Jobindex's results page (November 2026): one wrapper per ad, paid ads and ads found by its robot.
JOBINDEX_PAGE = """
<html><body><div id="result_list_box">
<div id="jobad-wrapper-h1700001" data-jobsearch_position="1"><div class="jobsearch-result"><div class="PaidJob">
  <div class="jix_toolbar jix_toolbar_top"><div class="jix-toolbar-top">
    <div class="jix-toolbar-top__company"><a href="https://nordkraft.example/">Nordkraft Systems A/S</a></div>
    <div class="jix-toolbar-top__top-controls"><div class="jobad-element-menu-share" data-share-tid="h1700001"
      data-share-title="Embedded Software Engineer" data-share-url="https://www.jobindex.dk/vis-job/h1700001"></div></div>
  </div></div>
  <div class="PaidJob-inner">
    <h4><a href="https://nordkraft.example/careers/embedded-software-engineer">Embedded Software Engineer</a></h4>
    <div class="jobad-element-area"><span class="jix_robotjob--area">Horsens</span>
      <div class="jobad-element-distance"><a class="jobad-element-dialog-link" href="#">Se rejsetid</a></div></div>
    <p>Du skriver firmware i C til vores styringer.</p><p>Du bliver en del af et team på otte.</p>
  </div>
  <div class="jix_toolbar"><div class="jix-toolbar__pub"><div class="jix-toolbar__pubdate">Indrykket:
    <time datetime="2026-11-18">18-11-2026</time></div></div></div>
</div></div></div>
<div id="jobad-wrapper-r14000002" data-jobsearch_position="2"><div><div class="jobsearch-result"><div class="jix_robotjob">
  <span class="jix_toolbar jix_toolbar_top"><div class="jix-toolbar-top">
    <div class="jix-toolbar-top__company">Vestby Kommune</div></div></span>
  <div class="jix_robotjob-inner">
    <h4><a href="https://www.jobindex.dk/c?t=r14000002&amp;ctx=j">Pædagog til børnehus</a></h4>
    <div class="jobad-element-area"><span class="jix_robotjob--area">Vejle</span></div>
    <p>Vi søger en pædagog.</p>
  </div>
  <span class="jix_toolbar"><div class="jix-toolbar__pubdate">Indrykket: <time datetime="2026-11-16">16-11-2026</time></div></span>
</div></div></div></div>
</div><a href="/jobsoegning?q=embedded&page=2">2</a></body></html>
"""

# Jobnet's find-job page: cards with hashed class endings that link straight to the employer or to LinkedIn.
JOBNET_PAGE = """
<html><body><main>
<div class="card-content"><div class="jobadcard_job-ad-card__n1mp5"><div class="jobadcard_job-content__Xcuzx">
  <div class="jobadcard_job-header__82pXY"><div class="jobadcard_job-title__K1pM_">
    <a class="jobadcard_link-block__W_Re7" id="job-11111111-aaaa-headline" href="https://dk.linkedin.com/jobs/view/it-supporter-at-fjordtek-123?refId=abc&amp;trackingId=def">
      <div class="jobadcard_job-title-container__IhOBl"><h4 class="h4">IT-supporter</h4></div>
      <p class="jobadcard_sub-header__2bpJR">Fjordtek ApS</p></a></div></div>
  <div><p class="jobadcard_description-paragraph__VTOnj">Du hjælper kollegerne med deres pc'er.</p></div>
  <div><div class="jobadcard_job-info-list__aA30l">
    <time class="jobadcard_details-text__muO3P" datetime="2026-12-13">Frist: 13. dec.</time>
    <span class="jobadcard_details-text__muO3P">Fuldtid</span>
    <span class="jobadcard_details-text__muO3P"><address>8700 Horsens</address></span>
    <time class="jobadcard_details-text__muO3P" datetime="2026-11-19">Publiceret 19. nov.</time></div></div>
</div></div></div>
<div class="card-content"><div class="jobadcard_job-ad-card__n1mp5"><div class="jobadcard_job-content__Xcuzx">
  <a class="jobadcard_link-block__W_Re7" id="job-22222222-bbbb-headline" href="https://jobs.aaboværk.example/job/42">
    <h4 class="h4">Automationstekniker</h4><p class="jobadcard_sub-header__2bpJR">Aabo Værk</p></a>
  <address>Danmark</address>
</div></div></div>
</main></body></html>
"""


def _run_queue():
    """Run the queued collect tasks the way the queue would."""
    out = []
    for t in db.q("SELECT * FROM tasks WHERE kind = 'collect_jobs' AND status = 'queued' ORDER BY id"):
        out.append(collect.store(json.loads(t["payload"])))
        db.update("tasks", t["id"], {"status": "done"})
    return out


def test_jobindex_cards_are_read_by_their_layout():
    items = collect.extract(JOBINDEX_PAGE, "https://www.jobindex.dk/jobsoegning/it?radius=50&page=2")
    assert [it["url"] for it in items] == ["https://www.jobindex.dk/vis-job/h1700001",
                                           "https://www.jobindex.dk/vis-job/r14000002"]
    first = items[0]
    assert first["title"] == "Embedded Software Engineer" and first["company"] == "Nordkraft Systems A/S"
    assert first["location"] == "Horsens" and first["posted_at"] == "2026-11-18"
    assert first["text"] == "Du skriver firmware i C til vores styringer. Du bliver en del af et team på otte."
    assert items[1]["company"] == "Vestby Kommune" and items[1]["location"] == "Vejle"


def test_jobnet_cards_are_read_by_their_layout():
    items = collect.extract(JOBNET_PAGE, "https://jobnet.dk/find-job")
    assert [(it["title"], it["company"], it["location"]) for it in items] == [
        ("IT-supporter", "Fjordtek ApS", "8700 Horsens"), ("Automationstekniker", "Aabo Værk", "Danmark")]
    assert items[0]["posted_at"] == "2026-11-19" and items[0]["deadline"] == "2026-12-13"
    assert items[0]["text"] == "Du hjælper kollegerne med deres pc'er."
    assert items[0]["url"].startswith("https://dk.linkedin.com/jobs/view/it-supporter-at-fjordtek-123")
    assert items[1]["posted_at"] == "" and items[1]["deadline"] == ""


def test_a_site_without_a_layout_is_not_guessed_at():
    assert collect.extract("<html><a href='/job/1'>Job</a></html>", "https://jobs.example.com/search") is None


def test_a_collected_page_is_screened_stored_and_not_taken_twice(fake):
    from career_agent import ingest, profile
    from .conftest import CV_TEXT

    doc_id, _ = ingest.import_bytes("cv.txt", CV_TEXT.encode())
    profile.analyze_document(doc_id)
    url = "https://www.jobindex.dk/jobsoegning/it?radius=50&page=2"
    r = collect.receive(JOBINDEX_PAGE, url)
    assert r["ok"] and r["found"] == 2 and r["new"] == 2 and "Jobindex, page 2: 2 jobs on the page, 2 new" in r["message"]
    assert _run_queue() == [{"listings": 2, "new": 2, "kept": 1}]

    kept = db.q1("SELECT * FROM jobs WHERE url = 'https://www.jobindex.dk/vis-job/h1700001'")
    assert kept["status"] == "new" and kept["posted_at"] == "2026-11-18" and kept["location"] == "Horsens"
    assert kept["company_name"] == "Nordkraft Systems A/S" and kept["parse_status"] == "pending"
    dropped = db.q1("SELECT * FROM jobs WHERE url = 'https://www.jobindex.dk/vis-job/r14000002'")
    assert dropped["status"] == "filtered" and dropped["notes"] == sources.FILTERED_NOTE

    src = db.q1("SELECT * FROM sources WHERE kind = 'collect'")
    assert src["name"] == "Collected from Jobindex" and src["url"] == "https://www.jobindex.dk/"
    assert src["jobs_found"] == 1 and src["last_status"] == "2 listings, 2 new, 1 kept for review"
    assert kept["source_id"] == src["id"]
    assert sources.due() == [], "a collected site is never checked on its own"
    assert sources.poll(src["id"]).get("skipped")

    again = collect.receive(JOBINDEX_PAGE, url)
    assert again["new"] == 0 and "all already in Career Agent" in again["message"]
    assert _run_queue() == [] and db.scalar("SELECT COUNT(*) FROM sources") == 1


def test_a_link_the_site_closes_to_robots_is_kept_for_you_to_open(fake, monkeypatch):
    rules = {"dk.linkedin.com": robots.parse("User-agent: *\nDisallow: /\n")}
    monkeypatch.setattr(robots, "rules_for", lambda url: rules.get(__import__("urllib.parse").parse.urlparse(url).netloc, []))
    r = collect.receive(JOBNET_PAGE, "https://jobnet.dk/find-job")
    assert r["found"] == 2 and r["new"] == 2 and r["message"].startswith("Jobnet: 2 jobs")
    _run_queue()
    locked = db.q1("SELECT * FROM jobs WHERE title = 'IT-supporter'")
    assert locked["link_locked"] == 1 and locked["parse_status"] == "skipped"
    assert locked["notes"].startswith("Collected from Jobnet.") and locked["deadline"] == "2026-12-13"
    assert "refId" not in locked["url"] and "trackingId" not in locked["url"]
    assert locked["description"] == "Du hjælper kollegerne med deres pc'er."
    open_ = db.q1("SELECT * FROM jobs WHERE title = 'Automationstekniker'")
    assert open_["link_locked"] == 0 and open_["source_id"] == locked["source_id"]


def test_a_page_with_no_job_cards_says_so(fake):
    r = collect.receive("<html><body><h1>Log ind</h1></body></html>", "https://www.jobindex.dk/jobsoegning")
    assert r["ok"] is False and r["found"] == 0 and "No jobs found on this Jobindex page" in r["message"]
    assert db.scalar("SELECT COUNT(*) FROM tasks") == 0 and db.scalar("SELECT COUNT(*) FROM sources") == 0


def test_any_other_site_asks_first_then_goes_through_the_link_picker(fake):
    page = "<html><body><ul><li><a href='/job/1'>Backend developer at Acme</a></li><li><a href='/about'>About us</a></li></ul></body></html>"
    r = collect.receive(page, "https://jobs.example.com/search?q=backend")
    assert r["ok"] and r["found"] is None and "jobs.example.com" in r["message"] and r["ask"]
    assert db.scalar("SELECT COUNT(*) FROM tasks") == 0 and db.scalar("SELECT COUNT(*) FROM sources") == 0, "nothing until you say yes"
    kept = collect._kept_page(r["ask"])
    assert kept and kept.exists()

    said_yes = collect.confirm(r["ask"])
    assert said_yes["ok"] and "jobs.example.com" in said_yes["message"]
    assert _run_queue() == [{"listings": 1, "new": 1, "kept": 1}]
    job = db.q1("SELECT * FROM jobs")
    assert job["url"] == "https://jobs.example.com/job/1" and job["title"] == "Job A"
    assert not kept.exists(), "the kept page is removed once it has been read"
    assert db.q1("SELECT name FROM sources")["name"] == "Collected from jobs.example.com"
    with pytest.raises(ValueError):
        collect.confirm(r["ask"])
    with pytest.raises(ValueError):
        collect.confirm("../../etc/passwd")


def test_the_bookmark_posts_from_the_job_site_and_needs_its_key(fake):
    c = TestClient(app)
    assert c.get("/collect").status_code == 200
    assert "didn&#39;t come along" in c.get("/collect/take").text
    site = {"origin": "https://www.jobindex.dk"}  # the form is posted from the job site's own tab
    body = {"url": "https://www.jobindex.dk/jobsoegning/it?radius=50", "html": JOBINDEX_PAGE}

    no_key = c.post("/collect/take", data=body, headers=site)
    assert no_key.status_code == 200 and "out of date" in no_key.text
    wrong = c.post("/collect/take", data={**body, "token": "guess"}, headers=site)
    assert "out of date" in wrong.text and db.scalar("SELECT COUNT(*) FROM tasks") == 0

    r = c.post("/collect/take", data={**body, "token": collect.token()}, headers=site)
    assert r.status_code == 200 and "2 jobs on the page, 2 new" in r.text and 'data-close="1"' in r.text
    assert db.scalar("SELECT COUNT(*) FROM tasks WHERE kind = 'collect_jobs'") == 1

    bad = c.post("/collect/take", data={"url": "javascript:alert(1)", "html": "<p>x</p>", "token": collect.token()}, headers=site)
    assert "isn&#39;t a web page" in bad.text and 'data-close=""' in bad.text
    # Every other address still refuses a post from another site.
    assert c.post("/collect/confirm", data={"page_id": "0" * 24}, headers=site).status_code == 403
    assert c.post("/todos/add", data={"title": "x"}, headers=site).status_code == 403


def test_a_page_from_an_unknown_site_waits_for_your_yes(fake):
    c = TestClient(app)
    page = "<html><body><a href='/job/1'>Backend developer at Acme</a></body></html>"
    r = c.post("/collect/take", data={"url": "https://jobs.example.com/search", "html": page, "token": collect.token()},
               headers={"origin": "https://jobs.example.com"})
    assert "Look for jobs on this page" in r.text and 'data-close=""' in r.text
    page_id = r.text.split('name="page_id" value="')[1].split('"')[0]
    assert db.scalar("SELECT COUNT(*) FROM tasks") == 0
    yes = c.post("/collect/confirm", data={"page_id": page_id}, headers={"origin": "http://testserver"})
    assert yes.status_code == 200 and "Looking for job ads" in yes.text
    assert db.scalar("SELECT COUNT(*) FROM tasks WHERE kind = 'collect_jobs'") == 1


def test_settings_offers_the_bookmark_and_lists_collected_sites(fake):
    collect.source_for("https://jobnet.dk/find-job")
    c = TestClient(app)
    page = c.get("/settings").text
    key = collect.token()
    assert 'href="javascript:(function(){var A=&#39;http://testserver&#39;,d=document;' in page and key in page
    assert "Collected from Jobnet" in page and "pages you collect with the bookmark" in page
    assert collect.bookmark("http://127.0.0.1:8765/").count("http://127.0.0.1:8765'") == 1
    assert "collect_jobs" in tasks.TASK_LABELS and "collect_jobs" in tasks.FETCH_TASKS

    c.post("/collect/new-key", headers={"origin": "http://testserver"})
    assert collect.token() != key and not collect.token_ok(key) and collect.token_ok(collect.token())
