"""Career sites watched directly: Workday job lists and results pages spread over several pages."""
from __future__ import annotations

import pytest

from career_agent import db, fetch, robots, sources

LEGO = "https://lego.wd103.myworkdayjobs.com/en-US/LEGO_External?Location=49ab&Location=77cd&q=engineer"


def test_a_workday_address_names_its_job_list_and_filters():
    wd = sources.workday_parts(LEGO)
    assert wd == {"api": "https://lego.wd103.myworkdayjobs.com/wday/cxs/lego/LEGO_External/jobs",
                  "site_url": "https://lego.wd103.myworkdayjobs.com/LEGO_External", "search": "engineer",
                  "facets": {"Location": ["49ab", "77cd"]}}
    assert sources.workday_parts("https://www.jobindex.dk/jobsoegning") is None
    assert sources.workday_parts("https://lego.wd103.myworkdayjobs.com/") is None
    assert sources.detect_kind(LEGO) == "workday"


class _Json:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


def _workday_site(monkeypatch, n_jobs: int):
    """A Workday list of n_jobs that, like the real one, says how many there are in its first answer only."""
    calls = []

    def post_json(url, payload, timeout=25):
        calls.append(payload)
        off = payload["offset"]
        posts = [{"title": f"Engineer {i}", "externalPath": f"/job/Billund/Engineer-{i}_{i:04d}",
                  "locationsText": "2 Locations" if i % 2 else "Billund"}
                 for i in range(off, min(off + payload["limit"], n_jobs))]
        return _Json({"total": n_jobs if off == 0 else 0, "jobPostings": posts})

    monkeypatch.setattr(fetch, "post_json", post_json)
    monkeypatch.setattr(sources, "PAGE_PAUSE", 0)
    return calls


def test_a_workday_list_is_read_to_the_end(monkeypatch):
    calls = _workday_site(monkeypatch, 47)
    items = sources._workday_items(LEGO, "LEGO")
    assert len(items) == 47 and len(calls) == 3 and [c["offset"] for c in calls] == [0, 20, 40]
    assert calls[0]["appliedFacets"] == {"Location": ["49ab", "77cd"]} and calls[0]["searchText"] == "engineer"
    first = items[0]
    assert first == {"url": "https://lego.wd103.myworkdayjobs.com/LEGO_External/job/Billund/Engineer-0_0000",
                     "title": "Engineer 0", "company": "LEGO", "location": "Billund", "text": ""}
    assert items[1]["location"] == "Billund", "'2 Locations' says nothing: the place comes from the address"


def test_a_workday_site_that_forbids_its_list_is_not_read(monkeypatch):
    monkeypatch.setattr(robots, "rules_for", lambda url: robots.parse("User-agent: *\nDisallow: /wday/"))
    with pytest.raises(fetch.FetchError, match="doesn't let robots"):
        sources._workday_items(LEGO, "LEGO")


def test_a_workday_source_is_checked_and_its_jobs_stored(fake, monkeypatch):
    _workday_site(monkeypatch, 3)
    sid = sources.add("LEGO", LEGO)
    assert db.q1("SELECT kind FROM sources WHERE id = ?", (sid,))["kind"] == "workday"
    result = sources.poll(sid)
    assert result["listings"] == 3 and result["new"] == 3
    job = db.q1("SELECT * FROM jobs WHERE url LIKE '%Engineer-0_0000'")
    assert job["company_name"] == "LEGO" and job["source_id"] == sid
    assert sources.poll(sid)["new"] == 0


VESTAS = "https://careers.vestas.com/search/?q=&locationsearch=Aarhus&sortDirection=desc"


def _page(job: str, pager: str = "") -> str:
    return (f'<html><body><table><tr><td><a href="/job/{job}/">Engineer {job} in Aarhus</a></td></tr></table>'
            f'<ul class="pagination">{pager}</ul></body></html>')


PAGER = ('<li><a href="?q=&amp;locationsearch=Aarhus&amp;sortDirection=desc&amp;startrow=10">2</a></li>'
         '<li><a href="?q=&amp;locationsearch=Aarhus&amp;sortDirection=desc&amp;startrow=20">3</a></li>'
         '<li><a href="?q=&amp;locationsearch=Aarhus&amp;sortDirection=asc">Sort</a></li>'
         '<li><a href="/search/?q=&amp;locationsearch=Aalborg&amp;startrow=10">Aalborg</a></li>')


def test_a_results_page_names_its_later_pages_and_nothing_else():
    assert sources.next_pages(_page("a", PAGER), VESTAS) == [VESTAS + "&startrow=10", VESTAS + "&startrow=20"]
    assert sources.next_pages(_page("a"), VESTAS) == []


class _Resp:
    def __init__(self, url, text):
        self.url, self.text, self.content = url, text, text.encode()
        self.headers = {"content-type": "text/html"}


def test_a_page_source_follows_its_pages_while_they_show_new_jobs(fake, monkeypatch):
    site = {VESTAS: _page("a", PAGER), VESTAS + "&startrow=10": _page("b", PAGER),
            VESTAS + "&startrow=20": _page("c", PAGER)}
    fetched = []

    def get(url, timeout=25):
        fetched.append(url)
        return _Resp(url, site[url])

    monkeypatch.setattr(fetch, "get", get)
    monkeypatch.setattr(sources, "PAGE_PAUSE", 0)
    sid = sources.add("Vestas", VESTAS, kind="page")
    first = sources.poll(sid)
    assert first["new"] == 3 and fetched == [VESTAS, VESTAS + "&startrow=10", VESTAS + "&startrow=20"]
    assert db.scalar("SELECT COUNT(*) FROM jobs WHERE source_id = ?", (sid,)) == 3

    fetched.clear()
    assert sources.poll(sid)["new"] == 0 and fetched == [VESTAS], "nothing new on page 1: the later pages wait"


class _Status:
    def __init__(self, status: int, headers: dict | None = None):
        self.status_code, self.headers, self.text = status, headers or {}, "ok"


def test_one_site_is_asked_at_its_own_pace_and_a_slow_down_is_obeyed(monkeypatch):
    naps: list[float] = []
    answers = [_Status(429, {"retry-after": "3"}), _Status(429), _Status(200)]
    monkeypatch.setattr(fetch, "SITE_GAP", 1.5)
    monkeypatch.setattr(fetch.time, "sleep", naps.append)
    monkeypatch.setattr(fetch.httpx2, "get", lambda *a, **k: answers.pop(0))
    fetch._next_free.clear()
    assert fetch.get("https://jobs.example.com/a").status_code == 200
    assert 3.0 in naps and 30 in naps, "waits as long as the site says, else 10 then 30 seconds"
    assert any(0 < n <= fetch.SITE_GAP for n in naps), "and never asks the same site faster than its gap"

    # Still told to slow down after two waits: a failure that is tried again later, not a blocked page.
    monkeypatch.setattr(fetch.httpx2, "get", lambda *a, **k: _Status(429))
    with pytest.raises(fetch.FetchError, match="slow down") as caught:
        fetch.get("https://jobs.example.com/b")
    from career_agent import tasks
    assert caught.value.later and tasks.is_transient(caught.value) and tasks.is_passing(str(caught.value))
    monkeypatch.setattr(fetch.httpx2, "get", lambda *a, **k: _Status(403))
    with pytest.raises(fetch.FetchError, match="blocked") as caught:
        fetch.get("https://jobs.example.com/c")
    assert not tasks.is_transient(caught.value)


def test_a_source_its_site_forbids_is_switched_off_once(monkeypatch):
    sid = db.insert("sources", {"name": "Closed", "kind": "rss", "url": "https://closed.example.com/jobs.rss", "created_at": db.now()})
    monkeypatch.setattr(robots, "rules_for", lambda url: robots.parse("User-agent: *\nDisallow: /"))
    monkeypatch.setattr(fetch, "get", lambda *a, **k: pytest.fail("a forbidden address must not be fetched"))
    assert "switched off" in sources.poll(sid)["skipped"]
    row = db.q1("SELECT enabled, last_status, last_error FROM sources WHERE id = ?", (sid,))
    assert row["enabled"] == 0 and row["last_status"] == "error" and "Switched off" in row["last_error"]
    assert sid not in [s["id"] for s in sources.due()], "and isn't checked again"
    db.ex("DELETE FROM sources WHERE id = ?", (sid,))
