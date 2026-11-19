"""Robots rules for job sources, and tracking links turned into an address the app may read."""
from __future__ import annotations

import pytest

from career_agent import collect, db, robots, sources

# The parts of Jobindex's robots.txt (November 2026) that matter for job searches.
JOBINDEX_ROBOTS = """
User-agent: *
Allow: /img/content/
Disallow: /c/
Disallow: /c?
Disallow: /cgi/
Disallow: /job/*?
Disallow: /jobsoegning/*/*/*?
Disallow: /jobsoegning/*?*&
Disallow: /jobsoegning?*&*&
Disallow: /jobsoegning*page=
Disallow: /jobsoegning*jobage=
Disallow: /jobsoegning*radius=
Disallow: /jobsoegning*geoareaid=
Disallow: /jobsoegning*?*+*+
Disallow: /jobsoegning/kort

User-agent: 008
User-agent: CariBot
Disallow: /
"""

OFIR_ROBOTS = "User-agent: *\nAllow: /.well-known/\nDisallow: /\n"


@pytest.fixture()
def site_rules(monkeypatch):
    rules = {"www.jobindex.dk": robots.parse(JOBINDEX_ROBOTS), "www.ofir.dk": robots.parse(OFIR_ROBOTS)}
    monkeypatch.setattr(robots, "rules_for", lambda url: rules.get(__import__("urllib.parse").parse.urlparse(url).netloc, []))
    return rules


@pytest.mark.parametrize("path, blocked", [
    ("/jobsoegning.rss?q=product+owner", False),
    ("/jobsoegning.rss?q=product+owner&area=storkbh", False),
    ("/jobsoegning/storkbh?q=product+owner", False),
    ("/jobsoegning.rss?q=product+owner&page=2", True),
    ("/jobsoegning.rss?q=senior+product+owner", True),
    ("/jobsoegning.rss?q=po&radius=25", True),
    ("/jobsoegning.rss?q=po&geoareaid=12", True),
    ("/jobsoegning/storkbh?q=po&area=x", True),
    ("/vis-job/h1234567", False),
    ("/c?t=h1234567&ctx=mail", True),
    ("/img/content/logo.png", False),
])
def test_jobindex_rules(path, blocked):
    rules = robots.parse(JOBINDEX_ROBOTS)
    assert (robots.blocking_rule(rules, "https://www.jobindex.dk" + path) is not None) == blocked


def test_rules_for_other_robots_are_ignored():
    rules = robots.parse(JOBINDEX_ROBOTS)
    assert ("/" not in {p for _, p in rules}), "the 008/CariBot block-all must not apply to everyone"
    assert robots.blocking_rule(robots.parse(OFIR_ROBOTS), "https://www.ofir.dk/job/123") == "/"
    assert robots.blocking_rule(robots.parse(OFIR_ROBOTS), "https://www.ofir.dk/.well-known/x") is None


def test_a_source_the_site_forbids_is_not_added(fake, site_rules):
    with pytest.raises(sources.NotAllowed, match="two search words"):
        sources.add("Too many words", "https://www.jobindex.dk/jobsoegning?q=senior+product+owner")
    with pytest.raises(sources.NotAllowed, match="Collect jobs bookmark"):
        sources.add("Ofir", "https://www.ofir.dk/jobs?q=po", kind="page")
    sid = sources.add("Region", "https://www.jobindex.dk/jobsoegning?q=product+owner&area=storkbh", kind="rss")
    assert db.q1("SELECT url FROM sources WHERE id = ?", (sid,))["url"].endswith("jobsoegning.rss?q=product+owner&area=storkbh")


def test_an_old_source_the_site_forbids_reports_why(fake, site_rules):
    sid = db.insert("sources", {"name": "old", "kind": "rss", "url": "https://www.jobindex.dk/jobsoegning.rss?q=po&radius=20",
                                "created_at": db.now()})
    assert "switched off" in sources.poll(sid)["skipped"]
    row = db.q1("SELECT enabled, last_error FROM sources WHERE id = ?", (sid,))
    assert "radius=" in row["last_error"] and "region" in row["last_error"] and row["enabled"] == 0


def test_tracking_links_become_the_ads_own_page_or_are_kept_for_you(fake, site_rules):
    assert collect.readable_link("https://www.jobindex.dk/c?t=h1234567&ctx=mail") == "https://www.jobindex.dk/vis-job/h1234567"
    assert collect.readable_link("https://www.jobindex.dk/c?ctx=mail&sig=abc") is None
    assert collect.readable_link("https://track.example/click?u=https%3A%2F%2Fwww.jobindex.dk%2Fvis-job%2Fr7654321") \
        == "https://track.example/click?u=https%3A%2F%2Fwww.jobindex.dk%2Fvis-job%2Fr7654321", "no rules there: open as is"
