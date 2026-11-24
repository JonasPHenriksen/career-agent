"""The fact check reads only what changed since the last one; the voice read leaves the sentences you kept alone."""
from __future__ import annotations

from fastapi.testclient import TestClient

from career_agent import db, settings, tasks, tracker, voice, writer
from career_agent.web.app import app

from .test_cv import _draft

TEAM = "Jeg har ledet et team på 12 udviklere."


def _sent(fake) -> list[str]:
    return [user for system, user, schema in fake.calls if schema is writer.CHECK_SCHEMA]


def _marked(user: str) -> list[str]:
    """The items the check was asked to judge, by their text."""
    return [line.split(") ", 1)[1] for line in user.splitlines() if line.split(" ", 1)[0].endswith("]*")]


def _claims(app_id: int) -> dict:
    return db.jload(tracker.app_with_job(app_id)["claims"], {})


def test_only_new_and_changed_sentences_go_to_the_model(fake):
    a = _draft()
    first = _claims(a["id"])
    assert first["kept"] == 0 and first["snapshot"]["items"], "the first check reads everything, and keeps a snapshot"
    assert next(c for c in first["claims"] if c["text"] == TEAM)["status"] == "unsupported"

    calls = len(_sent(fake))
    writer.run_checks(a["id"])  # nothing changed: no model at all
    assert len(_sent(fake)) == calls
    again = _claims(a["id"])
    assert again["summary"].startswith("Nothing changed in the") and again["counts"] == first["counts"]
    assert next(c for c in again["claims"] if c["text"] == TEAM)["status"] == "unsupported", "the flag stays as it was"

    old = "Jeg søger stillingen som product owner hos FinPay."
    db.update("applications", a["id"], {"letter": a["letter"].replace(old, "Jeg vil gerne arbejde med betalinger hos FinPay.")})
    writer.run_checks(a["id"])
    sent = _sent(fake)[-1]
    marked = _marked(sent)
    assert len(marked) == 2 and marked[0] == "Jeg vil gerne arbejde med betalinger hos FinPay."
    assert marked[1].startswith("I Danske Bank har jeg ansvaret"), "the sentence after a changed one is read again"
    assert "(cv)" not in sent, "the CV didn't change, so it isn't sent"
    assert f"] (letter) {TEAM}" in sent, "the rest of the letter is there as context, unmarked"
    now = _claims(a["id"])
    assert now["checked"] == 2 and now["summary"].startswith("Checked the 2 new or changed of")
    assert [c for c in now["claims"] if c["where"] == "cv"] == [c for c in first["claims"] if c["where"] == "cv"]

    writer.run_checks(a["id"], full=True)
    sent = _sent(fake)[-1]
    assert not _marked(sent) and "(cv)" in sent, "everything, as the first time"


def test_a_verdict_is_made_again_when_the_facts_it_rests_on_change(fake):
    a = _draft()
    db.insert("facts", {"category": "experience", "title": "Team lead", "organization": "Danske Bank",
                        "detail": "Led 12 developers.", "verified": 1, "created_at": db.now()})
    writer.run_checks(a["id"])
    assert _marked(_sent(fake)[-1]) == [TEAM], "a new fact may back what was flagged; the rest stands"

    db.update("facts", 1, {"detail": "Changed by hand."})  # the fact the backed sentences leaned on
    claims = _claims(a["id"])["claims"]
    leaned = [c["text"] for c in claims if 1 in c["fact_ids"] or c["status"] != "supported"]
    assert any(c.get("source") for c in claims), "one is backed by a passage in your documents, not by a fact"
    writer.run_checks(a["id"])
    assert sorted(_marked(_sent(fake)[-1])) == sorted(leaned), "what leaned on it, and what's still flagged"


def test_a_cv_line_under_a_renamed_role_counts_as_changed():
    cv = "## Erfaring\n### Product Owner — Danske Bank\n- Ledte teamet\n"
    renamed = cv.replace("Product Owner", "Scrum Master")

    def keys(text: str) -> set[str]:
        return {writer._unit_key(*u) for u in writer._check_units("", text)}

    assert writer.check_items("", cv) == writer.check_items("", renamed) == [("cv", "Ledte teamet")]
    assert keys(cv) != keys(renamed)


def test_the_check_buttons_offer_what_changed_and_everything(fake):
    a = _draft()
    c = TestClient(app)
    assert "Check what changed" in c.get(f"/applications/{a['id']}?tab=check").text
    r = c.post(f"/applications/{a['id']}/check", data={"full": "1"}, follow_redirects=False)
    assert "Fact-checking+everything+again" in r.headers["location"]
    assert db.jload(db.q1("SELECT payload FROM tasks WHERE kind = 'check_application' ORDER BY id DESC")["payload"], {})["full"]


def test_sentences_you_keep_as_they_are_are_not_suggested_again(fake):
    settings.set("style_guide", "## Voice in brief\nPlain and a little dry.")
    a = _draft()
    tasks.run_task(db.q1("SELECT * FROM tasks WHERE kind = 'read_voice' AND status = 'queued'"))
    a = tracker.app_with_job(a["id"])
    pitch = "Jeg søger stillingen som product owner hos FinPay."
    assert [n["text"] for n in voice.read_notes(a)["notes"]] == [pitch]

    c = TestClient(app)
    assert "Keep them all as they are" in c.get(f"/applications/{a['id']}?tab=voice").text
    r = c.post(f"/applications/{a['id']}/voice-keep", follow_redirects=False)
    assert "Kept+it+as+you+wrote+it" in r.headers["location"]
    a = tracker.app_with_job(a["id"])
    assert voice.left_sentences(a) == [pitch] and voice.read_notes(a)["notes"] == []

    letter = a["letter"].replace("Ledte et team på 8 udviklere.", "Jeg ledte et team.")  # revised elsewhere
    read = voice.read(letter, "da", voice.settled_sentences(a), voice.left_sentences(a))
    assert read["notes"] == [], "the next read doesn't suggest it again"
    user = next(u for sy, u, sc in reversed(fake.calls) if sc is voice.READ_SCHEMA)
    assert "For sentences [2] an earlier reading suggested a plainer version, and the candidate chose to keep" in user


def test_sentences_you_untick_are_kept_and_the_ticked_ones_rewritten(fake):
    settings.set("style_guide", "## Voice in brief\nPlain and a little dry.")
    a = _draft()
    tasks.run_task(db.q1("SELECT * FROM tasks WHERE kind = 'read_voice' AND status = 'queued'"))
    c = TestClient(app)
    c.post(f"/applications/{a['id']}/voice-revise", data={"use_2": "1"}, follow_redirects=False)
    a = tracker.app_with_job(a["id"])
    assert voice.settled_sentences(a) == ["Jeg vil gerne være product owner hos FinPay."]
    assert voice.left_sentences(a) == [], "a sentence you ticked isn't one you kept"
