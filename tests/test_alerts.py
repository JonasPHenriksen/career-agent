"""Robots rules for job sources, and job alerts read from a Mail mailbox."""
from __future__ import annotations

import pytest

from career_agent import alerts, db, robots, settings, sources

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
    with pytest.raises(sources.NotAllowed, match="job alert by email"):
        sources.add("Ofir", "https://www.ofir.dk/jobs?q=po", kind="page")
    sid = sources.add("Region", "https://www.jobindex.dk/jobsoegning?q=product+owner&area=storkbh", kind="rss")
    assert db.q1("SELECT url FROM sources WHERE id = ?", (sid,))["url"].endswith("jobsoegning.rss?q=product+owner&area=storkbh")


def test_an_old_source_the_site_forbids_reports_why(fake, site_rules):
    sid = db.insert("sources", {"name": "old", "kind": "rss", "url": "https://www.jobindex.dk/jobsoegning.rss?q=po&radius=20",
                                "created_at": db.now()})
    with pytest.raises(sources.fetch.FetchError, match="region"):
        sources.poll(sid)
    assert "radius=" in db.q1("SELECT last_error FROM sources WHERE id = ?", (sid,))["last_error"]


ALERT_EMAIL = """From: Jobindex <jobagent@jobindex.dk>
To: sara@example.com
Subject: 3 nye job til dig
MIME-Version: 1.0
Content-Type: multipart/alternative; boundary="b"

--b
Content-Type: text/plain; charset=utf-8

Product Owner, FinPay A/S
https://www.jobindex.dk/c?t=h1234567&ctx=mail

--b
Content-Type: text/html; charset=utf-8

<html><body>
<div><a href="https://www.jobindex.dk/c?t=h1234567&amp;ctx=mail">Product Owner</a> FinPay A/S, Copenhagen. Lead payments.</div>
<div><a href="https://www.jobindex.dk/c?ctx=mail&amp;sig=abc">Projektleder</a> Novo Nordisk, Bagsvaerd</div>
<div><a href="https://www.jobindex.dk/jobagent/afmeld/xyz">Stop denne jobagent</a></div>
</body></html>
--b--
"""


def test_tracking_links_become_the_ads_own_page_or_are_kept_for_you(fake, site_rules):
    links = alerts.email_links(ALERT_EMAIL)
    assert [l["text"] for l in links][:2] == ["Product Owner", "Projektleder"]
    assert alerts.readable_link("https://www.jobindex.dk/c?t=h1234567&ctx=mail") == "https://www.jobindex.dk/vis-job/h1234567"
    assert alerts.readable_link("https://www.jobindex.dk/c?ctx=mail&sig=abc") is None
    assert alerts.readable_link("https://track.example/click?u=https%3A%2F%2Fwww.jobindex.dk%2Fvis-job%2Fr7654321") \
        == "https://track.example/click?u=https%3A%2F%2Fwww.jobindex.dk%2Fvis-job%2Fr7654321", "no rules there: open as is"


def test_job_alerts_from_mail_are_screened_and_read_once(fake, site_rules, monkeypatch):
    reads = []

    def mailbox(name, since, limit=60):
        reads.append((name, since))
        return [{"id": "<m1@jobindex>", "subject": "3 nye job til dig", "sender": "jobagent@jobindex.dk",
                 "date": "2026-11-15T07:00:00Z", "source": ALERT_EMAIL}]

    monkeypatch.setattr(alerts, "read_mailbox", mailbox)

    def answer(system, user, schema, _orig=fake.answer):
        if schema is sources.PAGE_SCHEMA:
            assert "job-alert email" in system.lower() and "Projektleder" in user
            return {"jobs": [{"n": 0, "title": "Product Owner", "company": "FinPay A/S", "location": "København"},
                             {"n": 1, "title": "Projektleder", "company": "Novo Nordisk", "location": "Bagsværd"}]}
        if schema is sources.TRIAGE_SCHEMA:
            return {"keep": [{"n": 0, "reason": "fits"}, {"n": 1, "reason": "fits"}]}
        return _orig(system, user, schema)

    monkeypatch.setattr(fake, "answer", answer)
    sid = alerts.add("", "Job alerts")
    src = db.q1("SELECT * FROM sources WHERE id = ?", (sid,))
    assert src["kind"] == "mail" and src["name"] == "Job alerts: Job alerts"

    result = sources.poll(sid)
    assert result == {"emails": 1, "listings": 2, "new": 2, "kept": 2}
    readable = db.q1("SELECT * FROM jobs WHERE url = 'https://www.jobindex.dk/vis-job/h1234567'")
    assert readable["parse_status"] == "pending" and readable["company_name"] == "FinPay A/S"
    locked = db.q1("SELECT * FROM jobs WHERE title = 'Projektleder'")
    assert locked["parse_status"] == "skipped" and locked["notes"] == sources.UNREADABLE_NOTE
    assert "1 with a link to open yourself" in db.q1("SELECT last_status FROM sources WHERE id = ?", (sid,))["last_status"]

    # The next check reads from a little before the last good read, and skips the email it already read.
    page_calls = sum(1 for _, _, s in fake.calls if s is sources.PAGE_SCHEMA)
    assert sources.poll(sid) == {"emails": 0, "listings": 0, "new": 0, "kept": 0}
    assert sum(1 for _, _, s in fake.calls if s is sources.PAGE_SCHEMA) == page_calls
    assert reads[1][1] < db.q1("SELECT read_until FROM sources WHERE id = ?", (sid,))["read_until"]
    assert settings.get(f"alerts_seen:{sid}") == ["<m1@jobindex>"]


def test_mail_problems_are_shown_on_the_source(fake, monkeypatch):
    def broken(name, since, limit=60):
        raise alerts.AlertsError("There's no mailbox called “Job alerts” in Mail.")

    monkeypatch.setattr(alerts, "read_mailbox", broken)
    sid = alerts.add("Alerts", "Job alerts")
    with pytest.raises(alerts.AlertsError):
        sources.poll(sid)
    row = db.q1("SELECT * FROM sources WHERE id = ?", (sid,))
    assert row["last_status"] == "error" and "no mailbox" in row["last_error"] and row["read_until"] is None
