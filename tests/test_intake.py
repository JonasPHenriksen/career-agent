"""Reading full ads behind job-board teaser pages, paging back through feeds, and re-screening. No network."""
from __future__ import annotations

import pytest

from career_agent import db, fetch, jobs, sources, tasks

AD_BODY = "Vi søger en backendudvikler til vores team. " * 80  # well over the "short page" limit


class Page:
    def __init__(self, url: str, html: str = "", content_type: str = "text/html; charset=utf-8", content: bytes | None = None):
        self.url = url
        self.text = html
        self.content = content if content is not None else html.encode()
        self.headers = {"content-type": content_type}


def teaser(links: str, text: str = "Kort tekst om jobbet.") -> str:
    return f"<html><body><main><h1>Job</h1><p>{text}</p>{links}</main></body></html>"


def serve(monkeypatch, pages: dict[str, str]) -> list[str]:
    """Make fetch.get answer from a dict of canned pages; returns the list of fetched URLs."""
    log: list[str] = []

    def fake_get(url, timeout=25):
        log.append(url)
        if url not in pages:
            raise fetch.FetchError("Page not found. The ad may have been taken down.")
        return Page(url, pages[url])

    monkeypatch.setattr("career_agent.fetch.get", fake_get)
    return log


WRAPPER = "https://www.jobindex.dk/vis-job/h1"


def test_jobindex_wrapper_follows_the_see_job_button(monkeypatch):
    html = teaser('<a class="btn seejobdesktop" href="https://employer.example/ad/1">Se jobbet</a>')
    serve(monkeypatch, {WRAPPER: html, "https://employer.example/ad/1": f"<html><body><main>{AD_BODY}</main></body></html>"})
    r = fetch.ad_page(WRAPPER)
    assert str(r.url) == "https://employer.example/ad/1"
    assert len(fetch.html_to_text(r.text)[1]) > 2500


def test_board_wrapper_without_button_uses_headline_link(monkeypatch):
    html = teaser('<h3 class="job-title"><a href="https://www.it-jobbank.dk/jobannonce/h2/dev">Dev</a></h3>')
    wrapper = "https://www.it-jobbank.dk/vis-job/h2"
    serve(monkeypatch, {wrapper: html, "https://www.it-jobbank.dk/jobannonce/h2/dev": f"<html><body><main>{AD_BODY}</main></body></html>"})
    assert str(fetch.ad_page(wrapper).url).endswith("/jobannonce/h2/dev")


def test_failed_follow_falls_back_to_the_wrapper(monkeypatch):
    html = teaser('<a class="seejobdesktop" href="https://gone.example/ad">Se jobbet</a>')
    serve(monkeypatch, {WRAPPER: html})
    assert str(fetch.ad_page(WRAPPER).url) == WRAPPER


def test_follow_that_gives_less_text_keeps_the_wrapper(monkeypatch):
    html = teaser('<a class="seejobdesktop" href="https://x.example/ad">Se jobbet</a>', text="Teaser. " * 100)
    serve(monkeypatch, {WRAPPER: html, "https://x.example/ad": "<html><body><p>Cookies</p></body></html>"})
    assert str(fetch.ad_page(WRAPPER).url) == WRAPPER


def test_first_page_failure_is_still_an_error(monkeypatch):
    serve(monkeypatch, {})
    with pytest.raises(fetch.FetchError):
        fetch.ad_page(WRAPPER)


def test_short_page_with_one_external_job_link_is_followed(monkeypatch):
    page = "https://staffing.example/job/1"
    serve(monkeypatch, {page: teaser('<a href="https://client.example/careers/9">View job</a>'),
                        "https://client.example/careers/9": f"<html><body><main>{AD_BODY}</main></body></html>"})
    assert str(fetch.ad_page(page).url) == "https://client.example/careers/9"


def test_two_job_links_or_a_long_page_are_not_followed(monkeypatch):
    two = "https://list.example/jobs"
    long_page = "https://long.example/job"
    log = serve(monkeypatch, {
        two: teaser('<a href="https://a.example/1">View job</a><a href="https://b.example/2">View job</a>'),
        long_page: teaser('<a href="https://a.example/1">View job</a>', text=AD_BODY),
    })
    assert str(fetch.ad_page(two).url) == two
    assert str(fetch.ad_page(long_page).url) == long_page
    assert log == [two, long_page]


def test_never_more_than_two_hops(monkeypatch):
    def hop(nxt):
        return teaser(f'<a href="{nxt}">View job</a>')
    log = serve(monkeypatch, {
        "https://a.example/1": hop("https://b.example/2") .replace("Kort tekst", "Lidt mere tekst"),
        "https://b.example/2": hop("https://c.example/3").replace("Kort tekst", "Endnu mere tekst, længere " * 5),
        "https://c.example/3": hop("https://d.example/4").replace("Kort tekst", "Allermest tekst her " * 20),
        "https://d.example/4": teaser(""),
    })
    fetch.ad_page("https://a.example/1")
    assert log == ["https://a.example/1", "https://b.example/2", "https://c.example/3"]


def test_parse_reads_the_full_ad_but_keeps_the_stored_url(fake, monkeypatch):
    serve(monkeypatch, {WRAPPER: teaser('<a class="seejobdesktop" href="https://employer.example/ad/1">Se jobbet</a>'),
                        "https://employer.example/ad/1": f"<html><body><main>{AD_BODY}</main></body></html>"})
    jid, _ = jobs.add_from_url(WRAPPER, text="short teaser", queue=False)
    jobs.parse(jid)
    assert db.q1("SELECT url FROM jobs WHERE id = ?", (jid,))["url"] == WRAPPER
    assert "backendudvikler" in fake.calls[-1][1] and "employer.example/ad/1" in fake.calls[-1][1]


# --- paging back through feeds ---------------------------------------------------------------------------------

def feed(urls: list[str]) -> bytes:
    items = "".join(f"<item><title>Job {u}</title><link>{u}</link><description>d</description></item>" for u in urls)
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>{items}</channel></rss>'.encode()


class FeedResp:
    def __init__(self, content: bytes, url: str):
        self.content, self.text, self.url = content, content.decode(), url
        self.headers = {"content-type": "application/rss+xml"}


@pytest.fixture()
def no_pause(monkeypatch):
    monkeypatch.setattr("career_agent.sources.time.sleep", lambda s: None)


def test_page_url_only_for_feeds_known_to_page():
    assert sources._page_url("https://jobbank.dk/job/rss/?q=x", 3) == "https://jobbank.dk/job/rss/?q=x&page=3"
    assert sources._page_url("https://jobbank.dk/job/rss/?page=2", 4) == "https://jobbank.dk/job/rss/?page=4"
    assert sources._page_url("https://www.jobindex.dk/jobsoegning.rss?q=x", 2) is None  # robots.txt forbids page=
    assert sources._page_url("https://x.teamtailor.com/jobs.rss", 2) is None


def test_poll_pages_back_until_a_page_has_nothing_new(fake, monkeypatch, no_pause):
    pages = {
        "https://jobbank.dk/job/rss/": [f"https://jobbank.dk/job/{i}" for i in range(1, 4)],
        "https://jobbank.dk/job/rss/?page=2": [f"https://jobbank.dk/job/{i}" for i in range(4, 7)],
        "https://jobbank.dk/job/rss/?page=3": [f"https://jobbank.dk/job/{i}" for i in (5, 6)],  # all seen on page 2
    }
    asked = []
    monkeypatch.setattr("career_agent.fetch.get", lambda url, timeout=25: (asked.append(url), FeedResp(feed(pages[url]), url))[1])
    # The first two are already known from an earlier run; page 1 still has new ones, so the poll goes on.
    jobs.add_from_url("https://jobbank.dk/job/1", queue=False)
    sid = sources.add("Jobbank", "https://jobbank.dk/job/rss/", kind="rss")
    result = sources.poll(sid)
    assert asked == list(pages)[:3]
    assert result["new"] == 5 and db.q1("SELECT COUNT(*) AS n FROM jobs")["n"] == 6


def test_poll_does_not_page_a_feed_that_cannot(fake, monkeypatch, no_pause):
    asked = []
    url = "https://www.jobindex.dk/jobsoegning.rss?q=x"
    monkeypatch.setattr("career_agent.fetch.get", lambda u, timeout=25: (asked.append(u), FeedResp(feed(["https://e.example/1"]), u))[1])
    sources.poll(sources.add("Jobindex", url, kind="rss"))
    assert asked == [url]


def test_poll_screens_a_long_list_in_batches(fake, monkeypatch):
    urls = [f"https://e.example/job/{i}" for i in range(95)]
    monkeypatch.setattr("career_agent.fetch.get", lambda u, timeout=25: FeedResp(feed(urls), u))
    sizes = []

    def triage(items):
        sizes.append(len(items))
        return {0: ""}

    monkeypatch.setattr(sources, "_triage", triage)
    result = sources.poll(sources.add("Big", "https://e.example/feed.rss", kind="rss"))
    assert sizes == [40, 40, 15]
    assert result == {"listings": 95, "new": 95, "kept": 3}
    assert db.q1("SELECT COUNT(*) AS n FROM jobs WHERE status = 'filtered'")["n"] == 92


# --- screening again -------------------------------------------------------------------------------------------

def test_rescreen_moves_jobs_both_ways_and_leaves_others_alone(fake, monkeypatch):
    sid = sources.add("S", "https://e.example/feed.rss", kind="rss")

    def add(n, status, parse_status="skipped", source=sid, **extra):
        jid = db.insert("jobs", {"url": f"https://e.example/{n}", "source_id": source, "title": f"Job {n}",
                                 "company_name": "Acme", "description": "x" * 500, "status": status,
                                 "parse_status": parse_status, "notes": "Filtered out by quick screening." if status == "filtered" else None,
                                 "created_at": db.now(), **extra})
        return jid

    now_kept_filtered = add(1, "filtered")                    # model keeps it: back to new, queued
    kept_parsed = add(2, "filtered", parse_status="parsed")   # already read: just back to new
    now_dropped = add(3, "new", parse_status="pending")       # model drops it: filtered
    dismissed = add(4, "dismissed")
    shortlisted = add(5, "shortlisted")
    manual = add(6, "new", source=None)
    applied = add(7, "new")
    db.insert("applications", {"job_id": applied, "created_at": db.now()})

    seen = []

    def triage(items):
        seen.append(items)
        assert items[0]["text"] == "x" * 240
        return {0: "", 1: ""}  # the first two of the batch, in id order

    monkeypatch.setattr(sources, "_triage", triage)
    result = sources.rescreen()
    assert result == {"screened": 3, "now_kept": 2, "now_dropped": 1}
    row = lambda i: db.q1("SELECT * FROM jobs WHERE id = ?", (i,))
    assert row(now_kept_filtered)["status"] == "new" and row(now_kept_filtered)["parse_status"] == "pending"
    assert row(now_kept_filtered)["notes"] is None
    assert db.q1("SELECT 1 FROM tasks WHERE kind = 'parse_job' AND ref = ?", (f"job:{now_kept_filtered}",))
    assert row(kept_parsed)["status"] == "new" and row(kept_parsed)["parse_status"] == "parsed"
    assert not db.q1("SELECT 1 FROM tasks WHERE kind = 'parse_job' AND ref = ?", (f"job:{kept_parsed}",))
    assert row(now_dropped)["status"] == "filtered" and "quick screening" in row(now_dropped)["notes"]
    for untouched, status in ((dismissed, "dismissed"), (shortlisted, "shortlisted"), (manual, "new"), (applied, "new")):
        assert row(untouched)["status"] == status

    seen.clear()
    assert sources.rescreen([now_dropped])["screened"] == 1, "a list of ids narrows it down"
    assert len(seen) == 1 and len(seen[0]) == 1
