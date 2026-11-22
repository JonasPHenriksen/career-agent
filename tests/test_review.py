"""The review screen: job post, letter and CV side by side, the questions only you can answer, and your answers:
what you confirm put where it belongs in your career database, and a revised draft."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from career_agent import db, jobs, review, settings, tasks, tracker, writer
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


def _run(kind: str) -> dict:
    """Run the oldest queued task of this kind, the way the queue does."""
    row = db.q1("SELECT * FROM tasks WHERE kind = ? AND status = 'queued' ORDER BY id", (kind,))
    assert row, f"no {kind} queued"
    tasks.run_task(row)
    return db.q1("SELECT * FROM tasks WHERE id = ?", (row["id"],))


def test_corrections_and_assumptions_change_the_draft_but_not_your_career_database(fake):
    a = _draft()
    qs = {q["kind"]: q for q in review.questions(a)}
    before = db.scalar("SELECT COUNT(*) FROM facts")
    c = TestClient(app)
    r = c.post(f"/applications/{a['id']}/answers", follow_redirects=False, data={
        f"v_{qs['gap']['id']}": "no", f"t_{qs['gap']['id']}": "",
        f"v_{qs['assumed']['id']}": "yes", f"t_{qs['assumed']['id']}": "Betalinger er det, jeg helst vil arbejde med.",
        f"v_{qs['claim']['id']}": "no", f"t_{qs['claim']['id']}": "not my biggest i would say", "effort": "high"})
    assert "Revising" in r.headers["location"] and "1+kept+as+true+for+this+application+only" in r.headers["location"]
    assert db.scalar("SELECT COUNT(*) FROM facts") == before, "a correction or a motive for one job is no career fact"
    assert not db.q1("SELECT 1 FROM tasks WHERE kind = 'place_answers'"), "nothing to place"
    task = json.loads(db.q1("SELECT payload FROM tasks WHERE kind = 'revise_application' ORDER BY id DESC")["payload"])
    assert task["part"] == "letter" and task["model"] == "||high"
    assert "really is new to the candidate" in task["instructions"] and "This is not true" in task["instructions"]
    assert "What is true: not my biggest i would say" in task["instructions"] and "Betalinger er det" in task["instructions"]

    a = tracker.app_with_job(a["id"])
    assert db.jload(a["review"], {})["confirmed"] == ["You want to work with payments again."]
    assert "assumed" not in [q["kind"] for q in review.questions(a)], "a confirmed assumption isn't asked again"
    writer.revise(a["id"], "Shorter.")  # the writer assumes the same again: it stays confirmed, and the writer is told
    a = tracker.app_with_job(a["id"])
    assert "assumed" not in [q["kind"] for q in review.questions(a)]
    prompt = next(user for system, user, schema in reversed(fake.calls) if schema is writer.LETTER_SCHEMA)
    assert "confirmed these are true for this application" in prompt and "You want to work with payments again." in prompt

    r = c.post(f"/applications/{a['id']}/answers", follow_redirects=False, data={})
    assert "Answer+at+least+one" in r.headers["location"]


def test_what_you_confirm_is_placed_before_the_draft_is_checked_again(fake):
    a = _draft()
    db.ex("DELETE FROM tasks")
    q = next(q for q in review.questions(a) if q["kind"] == "claim")
    r = TestClient(app).post(f"/applications/{a['id']}/answers", follow_redirects=False,
                             data={f"v_{q['id']}": "yes", f"t_{q['id']}": ""})
    assert "Putting+1+answer+in+your+career+database" in r.headers["location"]
    assert [t["kind"] for t in db.q("SELECT kind FROM tasks")] == ["place_answers"], "the check waits for the placing"
    before = db.scalar("SELECT COUNT(*) FROM facts")
    done = _run("place_answers")
    assert done["status"] == "done" and db.q1("SELECT kind FROM tasks WHERE status = 'queued'")["kind"] == "check_application"
    new = db.q1("SELECT * FROM facts ORDER BY id DESC LIMIT 1")
    assert db.scalar("SELECT COUNT(*) FROM facts") == before + 1 and new["title"] == "Confirmed fact"
    assert (new["verified"], new["tags"], new["source"]) == (0, "your answer", "manual"), "a new fact waits for your tick"
    assert "the application for Product Owner" in new["evidence"] and q["text"] in new["evidence"]
    assert review.last_placed(f"app:{a['id']}") == "1 new fact to check under Recently added."
    assert "1 new fact to check under Recently added." in TestClient(app).get(f"/applications/{a['id']}?tab=review").text


def test_a_sentence_you_said_is_true_is_not_asked_about_again_after_a_revision(fake):
    a = _draft()
    db.ex("DELETE FROM tasks")
    first = next(q for q in review.questions(a) if q["kind"] == "claim")
    assert f'title="team size">{first["text"]}</mark>' in review.marked(a["letter"], a)
    TestClient(app).post(f"/applications/{a['id']}/answers", follow_redirects=False,
                         data={f"v_{first['id']}": "yes", f"t_{first['id']}": ""})
    a = tracker.app_with_job(a["id"])
    assert first["text"] in db.jload(a["review"], {})["confirmed"]
    assert first["text"] not in [q["text"] for q in review.questions(a)], "not asked again while it's being placed"
    assert "assumed" in [q["kind"] for q in review.questions(a)], "the questions you didn't answer are still there"
    assert f'title="team size">{first["text"]}</mark>' not in review.marked(a["letter"], a)

    _run("place_answers")  # what the check found missing goes with it, so the database can take that part too
    sent = next(user for system, user, schema in reversed(fake.calls) if schema is review.PLACE_SCHEMA)
    assert "What the fact check found the database doesn't say: team size" in sent

    writer.revise(a["id"], "Shorter.")  # the same sentence again: the check is told, and it isn't flagged
    a = tracker.app_with_job(a["id"])
    claim = next(c for c in db.jload(a["claims"], {})["claims"] if c["text"] == first["text"])
    assert (claim["status"], claim["note"]) == ("supported", "You confirmed this is true.")
    assert first["text"] not in [q["text"] for q in review.questions(a)]
    system = next(system for system, user, schema in reversed(fake.calls) if schema is writer.CHECK_SCHEMA)
    assert "confirmed these are true when reviewing this application" in system and f"- {first['text']}" in system


def test_a_gap_you_answered_is_not_asked_about_again_though_the_writer_names_it_again(fake):
    assert review._same_gap("XBRL, annual reports and accounting", "XBRL, annual reports and accounting domain")
    assert not review._same_gap("XBRL, annual reports and accounting", "Kubernetes")
    a = _draft()
    db.ex("DELETE FROM tasks")
    TestClient(app).post(f"/applications/{a['id']}/answers", follow_redirects=False, data={"v_gap": "no", "t_gap": ""})
    a = tracker.app_with_job(a["id"])
    assert db.jload(a["review"], {})["gap_answers"] == [{"text": "Kubernetes", "has": False}]
    assert "gap" not in [q["kind"] for q in review.questions(a)]

    writer.revise(a["id"], "Shorter.")  # the writer names the same gap again
    a = tracker.app_with_job(a["id"])
    assert db.jload(a["review"], {})["main_gap"] == "Kubernetes" and "gap" not in [q["kind"] for q in review.questions(a)]
    prompt = next(user for system, user, schema in reversed(fake.calls) if schema is writer.LETTER_SCHEMA)
    assert "are new to them. Say so plainly" in prompt and "- Kubernetes" in prompt, "the writer is told"

    rev = db.jload(a["review"], {})
    for gap, asked in (("Kubernetes experience", False), ("Go and gRPC", True)):  # reworded: no; another gap: yes
        db.update("applications", a["id"], {"review": db.jdump({**rev, "main_gap": gap})})
        assert ("gap" in [q["kind"] for q in review.questions(tracker.app_with_job(a["id"]))]) is asked, gap


def test_placing_adds_to_the_fact_it_belongs_to_and_leaves_out_what_isnt_your_career(fake):
    _draft()
    po = db.q1("SELECT * FROM facts WHERE title = 'Product Owner'")
    db.update("facts", po["id"], {"verified": 1})
    result = review.place([
        {"kind": "claim", "text": "Jeg byggede betalingsflowet i mobilbanken.", "said": "", "fact_ids": [po["id"]]},
        {"kind": "claim", "text": "Jeg har ansvaret for betalingerne.", "said": "already in there", "fact_ids": [po["id"]]},
        {"kind": "claim", "text": "Så jeg kan starte 1. februar 2027.", "said": "", "fact_ids": []},
        {"kind": "gap", "text": "Kubernetes", "said": "Ran our test cluster", "fact_ids": []},
    ], "the application for Product Owner")
    after = db.q1("SELECT * FROM facts WHERE id = ?", (po["id"],))
    assert after["detail"] == f"{po['detail']} Also: Jeg byggede betalingsflowet i mobilbanken." and after["verified"] == 1
    assert (result["added_to"], result["new"], result["known"], result["skipped"]) == (["Product Owner"], ["Kubernetes"], 1, 1)
    k8s = db.q1("SELECT * FROM facts WHERE title = 'Kubernetes'")
    assert k8s["category"] == "skill" and not k8s["verified"] and "(Ran our test cluster)" in k8s["evidence"]
    assert not db.q1("SELECT 1 FROM facts WHERE title LIKE '%starte%' OR detail LIKE '%starte%'")
    assert result["said"] == ("Added to “Product Owner”; 1 new fact to check under Recently added; "
                              "1 your career database already had; 1 not about your career, so not saved.")
    sent = next(user for system, user, schema in reversed(fake.calls) if schema is review.PLACE_SCHEMA)
    assert f"[#{po['id']}] Product Owner (Danske Bank)" in sent, "the facts the check linked are shown in full"


def test_the_fact_check_knows_when_you_can_start(fake):
    settings.update("profile", {"availability": "From 1 February 2027"})
    writer.check_claims("Jeg kan starte 1. februar 2027.", "")
    system = next(system for system, user, schema in reversed(fake.calls) if schema is writer.CHECK_SCHEMA)
    assert "The candidate lives in København. Their availability: From 1 February 2027 They said so themselves" in system


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


def test_an_ad_that_turns_down_ai_written_applications_raises_a_warning(fake):
    yes = ["Bemærk: ansøgninger skrevet med ChatGPT eller anden AI bliver sorteret fra.",
           "Please note that AI-generated applications will not be considered.",
           "We want to hear from you, so write your cover letter yourself and not with an LLM.",
           "Skriv ansøgningen med dine egne ord – vi kan godt se, når den er AI-genereret."]
    no = ["You will build AI applications for our customers and not only maintain them.",
          "Experience applying generative AI where it genuinely fits, including RAG architectures.",
          "We use AI tools daily; experience with ChatGPT or Copilot is a plus.",
          "Send din ansøgning og CV senest 1. december. Vi svarer ikke på henvendelser fra bureauer."]
    assert all(jobs.ai_policy_in(f"Om jobbet.\n{s}\nVi glæder os.") == s for s in yes)
    assert not any(jobs.ai_policy_in(s) for s in no)

    # An ad read before the model was asked about it: the rules find the sentence, and the review warns.
    a = _draft()
    c = TestClient(app)
    assert "AI-written applications" not in c.get(f"/applications/{a['id']}").text
    db.update("jobs", a["job_id"], {"description": JOB_TEXT + "\n" + yes[0]})
    for url in (f"/applications/{a['id']}", f"/applications/{a['id']}?tab=letter", f"/jobs/{a['job_id']}"):
        page = c.get(url).text
        assert "AI-written applications" in page and "bliver sorteret fra" in page, url

    # The model that reads the ad quotes the sentence; a quote for an ad that never mentions AI is dropped.
    said = jobs.add_from_text(JOB_TEXT + "\nVi vil høre fra dig, ikke fra ChatGPT.")
    jobs.parse(said, use_text=True)
    assert jobs.get(said)["ai_notice"] == "AI-skrevne ansøgninger læses ikke."
    plain = jobs.add_from_text(JOB_TEXT.replace("Product Owner", "Produktejer"))
    jobs.parse(plain, use_text=True)
    assert jobs.get(plain)["d"]["ai_policy"] == "" and jobs.get(plain)["ai_notice"] is None


def test_a_line_in_the_cv_data_is_changed_the_way_it_was_shown():
    data = {"summary": "Engineer. Built 20+ projects.",
            "work": [{"name": "Havbro", "position": "Intern", "start": "Jul 2024", "end": "Dec 2024", "location": "Horsens",
                      "highlights": ["Did A", "Did B"]}],
            "skills": [{"name": "Languages", "keywords": ["Go", "Java"]}]}
    assert review.replace_in_cv(data, "Jul 2024 – Dec 2024 · Horsens", "Aug 2024 – Dec 2024 · Horsens")
    assert (data["work"][0]["start"], data["work"][0]["end"], data["work"][0]["location"]) == ("Aug 2024", "Dec 2024", "Horsens")
    assert review.replace_in_cv(data, "Built 20+ projects.", "") and data["summary"] == "Engineer."
    assert review.replace_in_cv(data, "Did B", "") and data["work"][0]["highlights"] == ["Did A"]
    assert review.replace_in_cv(data, "Languages: Go, Java", "Languages: Go, C#") and data["skills"][0]["keywords"] == ["Go", "C#"]
    assert not review.replace_in_cv(data, "Never said this", "x")


def test_base_cv_lines_the_database_does_not_back_are_settled_once(fake):
    from career_agent import cvdata, tasks

    _draft()
    assert review.check_base()["claims"] and not review.base_questions(cvdata.base()), "this CV is where the facts came from"
    base = cvdata.base()
    lines = [text for where, text in writer.check_items("", cvdata.to_markdown(base["data"])) if len(text.split()) >= 4][:2]
    db.update("cv_bases", base["id"], {"claims": json.dumps({"checked_at": db.now(), "claims": [
        {"text": lines[0], "where": "cv", "status": "unsupported", "note": "not in the database"},
        {"text": lines[1], "where": "cv", "status": "partly", "note": "the database says otherwise"}]})})
    base = cvdata.base()
    qs = review.base_questions(base)
    assert [q["text"] for q in qs] == lines and all(q["where"] == "cv" for q in qs)
    c = TestClient(app)
    page = c.get("/profile?tab=cv").text
    assert "Does your CV agree with your career database?" in page and qs[0]["text"] in page

    facts = db.scalar("SELECT COUNT(*) FROM facts")
    db.ex("DELETE FROM tasks")
    r = c.post(f"/cv/base/{base['id']}/answers", follow_redirects=False, data={
        f"v_{qs[0]['id']}": "yes", f"t_{qs[0]['id']}": "", f"v_{qs[1]['id']}": "no", f"t_{qs[1]['id']}": "What is true instead"})
    assert r.status_code == 303 and "being+put+in+your+career+database" in r.headers["location"] and "changed" in r.headers["location"]
    text = cvdata.to_markdown(cvdata.base()["data"])
    assert "What is true instead" in text and qs[1]["text"] not in text
    assert [t["kind"] for t in tasks.active(ref="cv-base-check")] == ["place_answers"], "placed first, then checked again"
    _run("place_answers")
    assert db.scalar("SELECT COUNT(*) FROM facts") == facts + 1
    added = db.q1("SELECT title, evidence, verified FROM facts ORDER BY id DESC LIMIT 1")
    assert added["title"] == "Confirmed fact" and not added["verified"] and "your base CV" in added["evidence"]
    assert [t["kind"] for t in tasks.active(ref="cv-base-check")] == ["check_cv_base"]
    assert "1 new fact to check under Recently added." in c.get("/profile?tab=cv").text
    db.ex("DELETE FROM tasks")


def test_the_letter_is_read_for_your_voice_and_the_sentences_you_tick_are_rewritten(fake):
    from career_agent import voice

    settings.set("style_guide", "## Voice in brief\nPlain and a little dry.")
    a = _draft()
    assert [t["kind"] for t in tasks.active(ref=f"app:{a['id']}")].count("read_voice") == 1, "queued by the check"
    _run("read_voice")
    a = tracker.app_with_job(a["id"])
    read = voice.read_notes(a)
    assert read["summary"] == "Mostly plain; the opener is a pitch."
    assert [(n["n"], n["text"], n["plainer"]) for n in read["notes"]] == [
        (2, "Jeg søger stillingen som product owner hos FinPay.", "Jeg vil gerne være product owner hos FinPay.")]
    system, user = next((sy, u) for sy, u, sc in reversed(fake.calls) if sc is voice.READ_SCHEMA)
    assert "Plain and a little dry." in system and "[1] Kære Mette\n\n[2] Jeg søger" in user, "paragraphs kept, sentences numbered"
    assert not any("Kære Mette" in u for sy, u, sc in fake.calls if sc is voice.READ_SCHEMA and "CAREER DATABASE" in sy), \
        "the read doesn't carry the career database"

    c = TestClient(app)
    page = c.get(f"/applications/{a['id']}").text
    assert '<mark class="flag voice" title="a pitch opener. Plainer: Jeg vil gerne være product owner hos FinPay.">' in page
    assert "1 not in your voice" in page
    assert "1 sentence not in your voice" in c.get(f"/applications/{a['id']}?tab=letter").text
    page = c.get(f"/applications/{a['id']}?tab=voice").text
    assert "Mostly plain; the opener is a pitch." in page and 'name="use_2"' in page

    db.ex("DELETE FROM tasks")
    writer.run_checks(a["id"])  # checked again, same letter: the read is kept, not made again
    assert not db.q1("SELECT 1 FROM tasks WHERE kind = 'read_voice'") and voice.read_notes(tracker.app_with_job(a["id"]))

    r = c.post(f"/applications/{a['id']}/voice-revise", data={"use_2": "1", "effort": "high"}, follow_redirects=False)
    assert r.status_code == 303
    task = json.loads(db.q1("SELECT payload FROM tasks WHERE kind = 'revise_application'")["payload"])
    assert task["part"] == "letter" and task["model"] == "||high"
    assert "“Jeg søger stillingen som product owner hos FinPay.” → “Jeg vil gerne være product owner hos FinPay.”" in task["instructions"]
    assert "Tick+at+least" in c.post(f"/applications/{a['id']}/voice-revise", data={}, follow_redirects=False).headers["location"]

    db.update("applications", a["id"], {"letter": "Kære Mette\n\nNu står der noget helt andet i brevet.\n\nMed venlig hilsen\nSara Lund"})
    a = tracker.app_with_job(a["id"])
    assert voice.read_notes(a) is None, "a read of an older version isn't shown"
    assert "This version hasn't been read yet" in c.get(f"/applications/{a['id']}?tab=voice").text
    db.ex("DELETE FROM tasks")
    writer.run_checks(a["id"])
    assert db.q1("SELECT 1 FROM tasks WHERE kind = 'read_voice'"), "the new version is read at the next check"


def test_without_a_style_guide_the_letter_is_not_read_and_is_tried_again_later(fake):
    from career_agent import voice

    a = _draft()
    assert _run("read_voice")["status"] == "done"
    assert not any(sc is voice.READ_SCHEMA for _, _, sc in fake.calls), "nothing to compare with: no model call"
    assert voice.read_notes(tracker.app_with_job(a["id"]))["skipped"] == "no style guide yet"
    db.ex("DELETE FROM tasks")
    writer.run_checks(a["id"])
    assert db.q1("SELECT 1 FROM tasks WHERE kind = 'read_voice'"), "tried again once there may be a guide"
