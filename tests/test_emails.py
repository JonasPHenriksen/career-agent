"""Emails you send yourself, ready to copy with who they go to: the one your application goes with (Send tab),
asking whether a job is still open (the ad notices), and a follow-up when they haven't answered."""
from __future__ import annotations

from datetime import date, timedelta

from fastapi.testclient import TestClient

from career_agent import db, emails, jobs, mailer, settings, tracker
from career_agent.web.app import app

from .test_cv import _draft


def _you():
    settings.update("profile", {"name": "Sara Lund", "phone": "+45 12 34 56 78"})


def test_the_texts_are_plain_and_in_the_applications_language(fake):
    _you()
    assert emails.first_contact("Jacob Helding Lindkvist (IT-chef/HR); Helle Lund Gregersen (HR-direktør)") == "Jacob Helding Lindkvist"
    assert emails.first_contact("Mette Holm og Ole Hansen") == "Mette Holm" and emails.first_contact(None) == ""

    a = {"job_title": "Product Owner", "company_name": "FinPay A/S", "language": "da", "subject": "Ansøgning: Product Owner",
         "contact_name": "Mette Holm", "contact_email": "job@finpay.dk", "applied_at": f"{date.today().year}-03-04T12:00:00"}
    m = emails.application(a)
    assert m["to"] == "job@finpay.dk" and m["subject"] == "Ansøgning: Product Owner"
    assert m["body"] == ("Hej Mette\n\nHermed min ansøgning og mit CV til stillingen som Product Owner hos FinPay A/S.\n\n"
                         "Med venlig hilsen\nSara Lund\n+45 12 34 56 78")
    f = emails.followup(a)
    assert f["subject"] == "Opfølgning på min ansøgning: Product Owner" and "den 4. marts." in f["body"]
    assert "hvornår jeg kan forvente at høre fra jer" in f["body"] and not f.get("by_model")

    en = {**a, "language": "en", "subject": "", "contact_name": "", "contact_email": "", "contact_phone": "+45 99 88 77 66",
          "applied_at": "2025-12-01T12:00:00"}
    m = emails.application(en)
    assert m["subject"] == "Application: Product Owner" and m["body"].startswith("Hello,\n\nPlease find attached my cover letter")
    assert m["body"].endswith("Best regards\nSara Lund\n+45 12 34 56 78")
    assert m["to"] == "" and "no email address, but a phone number: +45 99 88 77 66" in m["no_address"]
    assert "on 1 December 2025." in emails.followup(en)["body"], "another year is named"

    drafted = emails.followup({**a, "followup_draft": "Subject: Status på min ansøgning\n\nHej Mette\n\nEr der nyt?"})
    assert (drafted["subject"], drafted["body"], drafted["by_model"]) == ("Status på min ansøgning", "Hej Mette\n\nEr der nyt?", True)

    job = {"title": "Udvikler", "language": "da", "deadline": "2030-01-01", "contact_name": "Anders Duelund", "contact_email": ""}
    ask = emails.still_open(job)
    assert ask["subject"] == "Er stillingen som Udvikler stadig ledig?" and ask["body"].startswith("Hej Anders\n\nJeg har set")
    assert "ansøgningsfrist" not in ask["body"], "it has a deadline: no need to ask for one"
    ask = emails.still_open({**job, "language": "en", "deadline": None, "contact_name": ""})
    assert ask["subject"] == "Is the Udvikler position still open?" and "is there a deadline for applications?" in ask["body"]


def test_the_send_tab_has_the_email_ready_with_the_address_to_copy(fake, monkeypatch):
    _you()
    a = _draft()
    page = TestClient(app).get(f"/applications/{a['id']}?tab=send").text
    assert '<details class="send-mail" open' in page, "the ad asks for applications by email"
    assert 'id="apply-to" type="text" value="job@finpay.dk"' in page and 'data-copy="#apply-to"' in page
    assert 'id="apply-subject" type="text" value="Ansøgning: Product Owner"' in page
    assert "Hej Mette\n\nHermed min ansøgning og mit CV til stillingen som Product Owner hos FinPay A/S." in page

    sent = []
    monkeypatch.setattr(mailer, "create_draft", lambda *args: sent.append(args))
    monkeypatch.setattr("career_agent.exporter.save_exports", lambda a: [])
    TestClient(app).post(f"/applications/{a['id']}/mail", follow_redirects=False)
    assert sent[0][:3] == (emails.application(a)["subject"], emails.application(a)["body"], "job@finpay.dk"), "Mail gets the same text"


def test_a_follow_up_is_noticed_when_due_and_ready_to_send(fake, monkeypatch):
    _you()
    a = _draft()
    c = TestClient(app)
    tracker.add_event(a["id"], "applied", occurred_at=db.days_ago(3))
    page = c.get(f"/applications/{a['id']}?tab=timeline").text
    assert "time for a short follow-up" not in page and 'id="followup"' in page, "ready, but not due yet"
    assert "ready for when it's time" in page

    db.update("applications", a["id"], {"applied_at": db.days_ago(12)})
    page = c.get(f"/applications/{a['id']}").text
    assert "Sent 12 days ago and no answer yet: time for a short follow-up." in page
    assert f'href="?tab=timeline#followup"' in page
    page = c.get(f"/applications/{a['id']}?tab=timeline").text
    assert 'id="fu-to" type="text" value="job@finpay.dk"' in page and "Opfølgning på min ansøgning: Product Owner" in page
    assert f'action="/applications/{a["id"]}/mail-followup"' in page and "Write it in your voice instead" in page
    assert f'/applications/{a["id"]}?tab=timeline#followup">Follow-up email</a>' in c.get("/").text, "on Today"

    sent = []
    monkeypatch.setattr(mailer, "create_draft", lambda *args: sent.append(args))
    c.post(f"/applications/{a['id']}/mail-followup", follow_redirects=False)
    assert sent[0][2:] == ("job@finpay.dk", []) and sent[0][0] == "Opfølgning på min ansøgning: Product Owner"

    c.post(f"/applications/{a['id']}/followup-sent", follow_redirects=False)
    page = c.get(f"/applications/{a['id']}?tab=timeline").text
    assert "time for a short follow-up" not in page and "You sent a follow-up" in page
    assert tracker.followup_due(tracker.app_with_job(a["id"])) is None


def test_an_ad_that_may_be_gone_offers_an_email_asking_whether_its_open(fake, monkeypatch):
    _you()
    a = _draft()
    job_id = a["job_id"]
    monkeypatch.setattr("career_agent.adcheck.queue_if_stale", lambda job: None)
    db.update("jobs", job_id, {"url": "https://finpay.example/job/1", "checked_at": db.now(), "check_note": "up on finpay.example"})
    c = TestClient(app)
    for url in (f"/applications/{a['id']}", f"/jobs/{job_id}"):
        assert "asking whether it's still open" not in c.get(url).text, "seen up: nothing to ask"

    db.update("jobs", job_id, {"check_note": "couldn't tell from finpay.example (the page may be built in the browser)"})
    for url in (f"/applications/{a['id']}", f"/jobs/{job_id}"):
        page = c.get(url).text
        assert '<details class="ask-open"  style' in page, url + ": folded away when the check couldn't tell"
        assert f'id="ask-{job_id}-to" type="text" value="job@finpay.dk"' in page and "Er stillingen som Product Owner stadig ledig?" in page

    db.update("jobs", job_id, {"gone_at": db.now(), "check_note": "finpay.example says \"job not found\""})
    page = c.get(f"/applications/{a['id']}").text
    assert "This job ad is gone." in page and page.count(f'id="ask-{job_id}-subject"') == 1

    old = (date.today() - timedelta(days=200)).isoformat()
    db.update("jobs", job_id, {"gone_at": None, "deadline": None, "posted_at": old,
                               "check_note": "couldn't tell from finpay.example (the page may be built in the browser)"})
    page = c.get(f"/applications/{a['id']}").text
    assert "months without a deadline" in page and '<details class="ask-open" open' in page
    assert page.count(f'id="ask-{job_id}-subject"') == 1, "asked once, in the card, not also under the check line"
    assert "Og hvis den er, er der så en ansøgningsfrist?" in page

    sent = []
    monkeypatch.setattr(mailer, "create_draft", lambda *args: sent.append(args))
    c.post(f"/jobs/{job_id}/mail-ask", follow_redirects=False)
    assert sent[0][0] == "Er stillingen som Product Owner stadig ledig?" and sent[0][2:] == ("job@finpay.dk", [])
    assert jobs.get(job_id)["contact_email"] == "job@finpay.dk"
