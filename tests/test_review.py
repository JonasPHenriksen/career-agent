"""The review screen: job post, letter and CV side by side, the questions only you can answer, and your answers
becoming facts and a revised draft."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from career_agent import db, jobs, review, tracker, writer
from career_agent.web.app import app

from .conftest import JOB_TEXT
from .test_cv import _draft


def test_a_draft_opens_on_the_review_screen(fake):
    a = _draft()
    c = TestClient(app)
    page = c.get(f"/applications/{a['id']}").text
    assert 'aria-label="The job post"' in page and 'aria-label="The letter"' in page and 'aria-label="The CV"' in page
    assert "Erfaring med Scrum" in page and "Product Owner til vores betalingsteam" in page, "must-haves and the ad itself"
    assert f"/applications/{a['id']}/cv-page/1.png" in page, "the typeset CV"
    assert '<mark class="flag unsupported" title="team size">Jeg har ledet et team på 12 udviklere.</mark>' in page
    assert "The writer's angle:" in page and "Payments product owner with a measurable result." in page
    assert "Draft 1 of" not in page, "one draft: nothing to step through"
    assert c.get(f"/applications/{a['id']}?tab=letter").text.count('name="letter"') == 1, "the other tabs still work"


def test_questions_come_from_the_gap_the_assumptions_and_the_fact_check(fake):
    a = _draft()
    qs = review.questions(a)
    assert [q["kind"] for q in qs][:2] == ["gap", "assumed"] and qs[0]["text"] == "Kubernetes"
    assert qs[1]["text"] == "You want to work with payments again."
    claim = next(q for q in qs if q["kind"] == "claim")
    assert claim["text"] == "Jeg har ledet et team på 12 udviklere." and claim["status"] == "unsupported" and claim["where"] == "letter"
    assert qs[0]["detail"] == "The letter doesn't mention it. Suggested sentence: “Kubernetes har jeg ikke arbejdet med endnu.”"


def test_your_answers_become_facts_and_the_draft_is_revised(fake):
    a = _draft()
    qs = {q["kind"]: q for q in review.questions(a)}
    before = db.scalar("SELECT COUNT(*) FROM facts")
    c = TestClient(app)
    r = c.post(f"/applications/{a['id']}/answers", follow_redirects=False, data={
        f"v_{qs['gap']['id']}": "no", f"t_{qs['gap']['id']}": "",
        f"v_{qs['assumed']['id']}": "yes", f"t_{qs['assumed']['id']}": "Betalinger er det, jeg helst vil arbejde med.",
        f"v_{qs['claim']['id']}": "no", f"t_{qs['claim']['id']}": "Teamet var på 8 udviklere.", "effort": "high"})
    assert "2+answers+saved" in r.headers["location"] and "Revising" in r.headers["location"]
    new = db.q("SELECT category, title, detail, verified, source, evidence FROM facts ORDER BY id DESC LIMIT ?",
               (db.scalar("SELECT COUNT(*) FROM facts") - before,))
    assert [(f["category"], f["title"]) for f in new] == [("achievement", "Teamet var på 8 udviklere."),
                                                         ("value", "You want to work with payments again.")]
    assert new[1]["detail"].startswith("Betalinger er det") and all(f["verified"] and f["source"] == "manual" for f in new)
    task = json.loads(db.q1("SELECT payload FROM tasks WHERE kind = 'revise_application' ORDER BY id DESC")["payload"])
    assert task["part"] == "letter" and task["model"] == "||high"
    assert "really is new to the candidate" in task["instructions"] and "This is not true" in task["instructions"]
    assert "What is true: Teamet var på 8 udviklere." in task["instructions"] and "Betalinger er det" in task["instructions"]

    r = c.post(f"/applications/{a['id']}/answers", follow_redirects=False, data={})
    assert "Answer+at+least+one" in r.headers["location"]
    db.ex("DELETE FROM tasks")
    result = review.apply_answers(a, {qs["claim"]["id"]: {"verdict": "yes", "text": ""}})
    assert result == {"facts": 1, "revised": False}, "a plain confirmation only needs the facts checked again"
    assert db.q1("SELECT kind FROM tasks")["kind"] == "check_application"


def test_other_applications_to_the_same_employer_are_shown_and_avoided(fake):
    a = _draft()
    second = jobs.add_from_text(JOB_TEXT + "\nEn anden stilling i samme firma.")
    jobs.parse(second, use_text=True)
    other = jobs.create_application(second, draft=False)
    assert "also applied to this employer" in writer._letter_task(jobs.get(second), "da", None)
    assert "Payments product owner with a measurable result." in writer._siblings_note(jobs.get(second)), "the first letter's angle"
    assert writer._siblings_note(jobs.get(a["job_id"])) == "", "an application without a letter isn't something to avoid"
    writer.draft(other)
    page = TestClient(app).get(f"/applications/{other}").text
    assert "You have 1 other application for FinPay A/S" in page and f'href="/applications/{a["id"]}"' in page
    assert "Draft 1 of 2 waiting for you" in page or "Draft 2 of 2 waiting for you" in page
    assert tracker.app_with_job(other)["review"]
