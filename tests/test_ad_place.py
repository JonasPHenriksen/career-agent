"""Every ad says where the job is: in its text, on the job board's page, or on the results page that found it."""
from __future__ import annotations

from career_agent import db, fetch, jobs

AD = ("<html><body><h1>Udvikler</h1><p>" + "Vi søger en udvikler til vores team. " * 30 + "</p>{extra}</body></html>")


def _page(monkeypatch, html, url="https://board.example/job/1"):
    class Resp:
        text = html

    Resp.url = url
    monkeypatch.setattr(fetch, "ad_page", lambda u: Resp())


def _no_place_in_the_ad(fake):
    base = fake.answer
    fake.answer = lambda system, user, schema: ({**base(system, user, schema), "location": ""}
                                                if schema is jobs.PARSE_SCHEMA else base(system, user, schema))


def test_the_job_boards_own_line_or_map_gives_the_place_the_ad_text_leaves_out(fake, monkeypatch):
    _no_place_in_the_ad(fake)
    _page(monkeypatch, AD.format(extra='<div class="jix_robotjob--area">\n Hedensted </div>'))
    job_id, _ = jobs.add_from_url("https://board.example/job/1")
    jobs.parse(job_id)
    assert jobs.get(job_id)["location"] == "Hedensted"
    reading = next(user for _, user, schema in fake.calls if schema is jobs.PARSE_SCHEMA)
    assert "Where the job board lists the job (outside the ad text): Hedensted" in reading

    assert fetch.board_location('"title":"8260 Viby J","type":"area"} "title":"\\u00c5rhus N","type":"area"') \
        == "8260 Viby J, Århus N", "the map of the job's area, written as JSON"
    assert fetch.board_location('<svg><title>Lokation:</title><use href="#geo"></use></svg> Hedensted </div>'
                                '"title":"Nilanvej 2, 8722 Hedensted","type":"address"') \
        == "Hedensted, Nilanvej 2, 8722 Hedensted", "the 'Lokation' line and the address on the map"
    assert fetch.board_location("<p>Nothing about the place</p>") == ""


def test_reading_an_ad_never_wipes_out_the_place_its_results_page_gave(fake, monkeypatch):
    _no_place_in_the_ad(fake)
    _page(monkeypatch, AD.format(extra=""), "https://board.example/job/2")
    job_id, _ = jobs.add_from_url("https://board.example/job/2")
    db.update("jobs", job_id, {"location": "Skanderborg"})  # what the results page said (sources._store)
    jobs.parse(job_id)
    assert jobs.get(job_id)["location"] == "Skanderborg"

    _page(monkeypatch, AD.format(extra=""), "https://board.example/job/4")
    other, _ = jobs.add_from_url("https://board.example/job/4")
    jobs.parse(other)
    assert jobs.get(other)["location"] == "Skanderborg", "nothing anywhere: where the employer's other ads say"


def test_a_teaser_is_neither_read_nor_scored_and_asks_for_the_ad(fake, monkeypatch):
    def locked(url):
        raise fetch.FetchError("The page needs you to log in")

    monkeypatch.setattr(fetch, "ad_page", locked)
    teaser = "Krüger Search søger en teknisk dygtig IT-udvikler indenfor professionelle e-commerce løsninger."
    job_id, _ = jobs.add_from_url("https://board.example/job/3", text=teaser, queue=False)
    try:
        jobs.parse(job_id)
        raise AssertionError("a teaser must not be read")
    except fetch.FetchError as e:
        assert "Only a teaser of the ad could be read" in str(e)
    job = jobs.get(job_id)
    assert job["parse_status"] == "error" and job["fit_score"] is None
    assert "Paste the full ad text" in jobs.needs_text(job)
    assert not [c for c in fake.calls if c[2] in (jobs.PARSE_SCHEMA, jobs.FIT_SCHEMA)], "no model asked"

    _page(monkeypatch, AD.format(extra=""), "https://board.example/job/3")
    pasted = jobs.add_from_text("Udvikler hos Havbro. Kort opslag.", url="https://board.example/job/3")
    jobs.parse(pasted, use_text=True)
    assert jobs.get(pasted)["parse_status"] == "parsed", "text you paste yourself is read, however short"


def test_a_jobindex_feed_item_brings_its_place_along():
    from career_agent import sources

    rss = ('<?xml version="1.0"?><rss version="2.0"><channel><item><title>Data Scientist, Brinkholm</title>'
           '<link>https://www.jobindex.dk/vis-job/h1</link><description>&lt;div class="jobad-element-area"&gt;'
           '&lt;span class="jix_robotjob--area"&gt;Aarhus C&lt;/span&gt;&lt;/div&gt;&lt;p&gt;We are hiring.&lt;/p&gt;'
           '</description></item></channel></rss>').encode()
    item = sources._rss_items(rss, "https://www.jobindex.dk/jobsoegning.rss?q=python")[0]
    assert (item["title"], item["company"], item["location"]) == ("Data Scientist", "Brinkholm", "Aarhus C")
    assert "We are hiring." in item["text"]


def test_an_employer_page_marked_up_for_search_engines_gives_the_jobs_place_not_the_companys():
    page = ('<span itemprop="hiringOrganization"><meta itemprop="streetAddress" content="Head office, 7330 Brande">'
            '</span><span itemprop="jobLocation" itemscope itemtype="http://schema.org/Place"><span itemprop="address">'
            '<meta itemprop="streetAddress" content="Inge Lehmanns Gade 2, Aarhus C, 8000, Denmark"></span></span>')
    assert fetch.board_location(page) == "Inge Lehmanns Gade 2, Aarhus C, 8000, Denmark"


def test_a_gone_ad_is_marked_gone_and_its_stored_text_is_never_read(fake, monkeypatch):
    def gone(url):
        raise fetch.FetchError("Page not found. The ad may have been taken down.", gone=True)

    monkeypatch.setattr(fetch, "ad_page", gone)
    job_id, _ = jobs.add_from_url("https://board.example/job/9", text="Full stack developer hos Havbro. " * 40,
                                  queue=False)
    assert jobs.parse(job_id) == {"title": None, "skipped": "ad gone"}
    job = jobs.get(job_id)
    assert job["gone_at"] and job["parse_status"] == "error" and job["fit_score"] is None
    assert not [c for c in fake.calls if c[2] is jobs.PARSE_SCHEMA], "the old feed text is not read"
    assert jobs.needs_text(job) is None, "a gone ad doesn't ask you to paste it"
