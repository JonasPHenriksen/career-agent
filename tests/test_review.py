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


def test_what_you_confirm_is_suggested_and_nothing_changes_until_you_approve_it(fake):
    a = _draft()
    db.ex("DELETE FROM tasks")
    q = next(q for q in review.questions(a) if q["kind"] == "claim")
    c = TestClient(app)
    r = c.post(f"/applications/{a['id']}/answers", follow_redirects=False, data={f"v_{q['id']}": "yes", f"t_{q['id']}": ""})
    assert "Suggesting+1+answer+for+your+career+database" in r.headers["location"]
    assert sorted(t["kind"] for t in db.q("SELECT kind FROM tasks")) == ["check_application", "place_answers"], \
        "the check doesn't wait: the database doesn't change until you approve"
    before = db.scalar("SELECT COUNT(*) FROM facts")
    assert _run("place_answers")["status"] == "done"
    assert db.scalar("SELECT COUNT(*) FROM facts") == before, "nothing goes in by itself"
    s = db.q1("SELECT * FROM fact_suggestions WHERE status = 'pending'")
    assert (s["action"], s["title"], s["origin"], s["ref"]) == ("new", "Confirmed fact", "answers", f"app:{a['id']}")
    assert json.loads(s["data"])["tags"] == "your answer"
    assert "the application for Product Owner" in s["why"] and q["text"] in s["why"]
    said = "Suggested 1 new fact. Nothing changes until you approve it under Profile → Career database → Pending changes."
    assert review.last_placed(f"app:{a['id']}") == said
    assert said in c.get(f"/applications/{a['id']}?tab=review").text

    page = c.get("/profile?tab=facts").text
    assert "1 pending" in page and f'id="suggestion-{s["id"]}"' in page and "New fact" in page
    r = c.post(f"/suggestions/{s['id']}/approve", follow_redirects=False)
    assert "Added+to+your+career+database" in r.headers["location"]
    new = db.q1("SELECT * FROM facts ORDER BY id DESC LIMIT 1")
    assert (new["title"], new["verified"], new["source"]) == ("Confirmed fact", 1, "manual"), "approved is checked by you"
    assert db.q1("SELECT context FROM change_ops ORDER BY id DESC LIMIT 1")["context"] == \
        "Approved a suggested change: “Confirmed fact”", "History names what you did"
    assert "pending" not in c.get("/profile?tab=facts").text.split("<nav", 1)[1].split("</nav>", 1)[0]


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


def test_answers_suggest_an_addition_to_the_fact_they_belong_to_and_leave_out_what_isnt_your_career(fake):
    from career_agent import suggestions

    _draft()
    po = db.q1("SELECT * FROM facts WHERE title = 'Product Owner'")
    items = [
        {"kind": "claim", "text": "Jeg byggede betalingsflowet i mobilbanken.", "said": "", "fact_ids": [po["id"]]},
        {"kind": "claim", "text": "Jeg har ansvaret for betalingerne.", "said": "already in there", "fact_ids": [po["id"]]},
        {"kind": "claim", "text": "Så jeg kan starte 1. februar 2027.", "said": "", "fact_ids": []},
        {"kind": "gap", "text": "Kubernetes", "said": "Ran our test cluster", "fact_ids": []},
    ]
    result = review.place(items, "the application for Product Owner", "app:1")
    assert db.q1("SELECT detail FROM facts WHERE id = ?", (po["id"],))["detail"] == po["detail"], "not changed by itself"
    assert (result["added_to"], result["new"], result["known"], result["skipped"]) == (["Product Owner"], ["Kubernetes"], 1, 1)
    edit = db.q1("SELECT * FROM fact_suggestions WHERE action = 'edit' AND status = 'pending'")
    assert edit["fact_id"] == po["id"] and edit["adds"] == "Also: Jeg byggede betalingsflowet i mobilbanken."
    k8s = db.q1("SELECT * FROM fact_suggestions WHERE action = 'new' AND status = 'pending'")
    assert k8s["title"] == "Kubernetes" and json.loads(k8s["data"])["category"] == "skill" and "(Ran our test cluster)" in k8s["why"]
    assert not db.q1("SELECT 1 FROM fact_suggestions WHERE title LIKE '%starte%' OR data LIKE '%starte%'")
    assert result["said"] == ("Suggested an addition to “Product Owner”; suggested 1 new fact; 1 your career database "
                              "already had; 1 not about your career, so not saved. Nothing changes until you approve it "
                              "under Profile → Career database → Pending changes.")
    sent = next(user for system, user, schema in reversed(fake.calls) if schema is review.PLACE_SCHEMA)
    assert f"[#{po['id']}] Product Owner (Danske Bank)" in sent, "the facts the check linked are shown in full"

    again = review.place(items[:1], "the application for Product Owner", "app:1")
    assert again["added_to"] == [] and suggestions.count() == 2, "not suggested twice"

    page = TestClient(app).get("/profile?tab=facts").text
    assert "2 pending" in page and "Addition to a fact" in page and "Also: Jeg byggede betalingsflowet" in page
    db.update("facts", po["id"], {"detail": "Rewritten by hand."})  # you change the fact meanwhile
    assert suggestions.waiting()[0]["stale"]
    suggestions.approve(edit["id"])
    after = db.q1("SELECT * FROM facts WHERE id = ?", (po["id"],))
    assert after["detail"] == "Rewritten by hand. Also: Jeg byggede betalingsflowet i mobilbanken." and after["verified"] == 1, \
        "the addition goes onto the fact as it is now"
    suggestions.refuse(k8s["id"])
    assert not db.q1("SELECT 1 FROM facts WHERE title = 'Kubernetes'")
    assert review.place(items[3:], "the application for Product Owner")["new"] == [], "a refused fact isn't suggested again"


def test_document_reading_and_tidy_up_suggest_and_wait_for_you(fake):
    from career_agent import ingest, profile, suggestions

    from .conftest import CV_TEXT

    doc_id, _ = ingest.import_bytes("cv.txt", CV_TEXT.encode())
    profile.analyze_document(doc_id)
    assert not db.scalar("SELECT COUNT(*) FROM facts"), "nothing goes in by itself"
    waiting = db.scalar("SELECT COUNT(*) FROM fact_suggestions WHERE document_id = ? AND status = 'pending'", (doc_id,))
    assert waiting > 1
    profile.analyze_document(doc_id)  # read again: what it said is already waiting, so nothing doubles
    assert db.scalar("SELECT COUNT(*) FROM fact_suggestions WHERE status = 'pending'") == waiting
    c = TestClient(app)
    assert f"{waiting} waiting" in c.get(f"/documents/{doc_id}").text
    page = c.get("/profile?tab=facts").text
    assert f"Approve all {waiting}" in page and f"{waiting} pending" in page
    first = db.q1("SELECT * FROM fact_suggestions WHERE status = 'pending' ORDER BY id")
    r = c.post(f"/suggestions/{first['id']}/approve", follow_redirects=False,
               data={"edited": "1", "title": "Edited before approving", "category": "skill", "detail": ""})
    assert "Added" in r.headers["location"]
    assert db.q1("SELECT title, category, verified FROM facts") == {"title": "Edited before approving", "category": "skill",
                                                                   "verified": 1}
    r = c.post(f"/suggestions/document/{doc_id}/approve", follow_redirects=False)
    assert db.scalar("SELECT COUNT(*) FROM facts") == waiting and not suggestions.count()

    # Two facts that look like the same thing: a merge to approve, not a merge done
    a, b = (r["id"] for r in db.q("SELECT id FROM facts ORDER BY id LIMIT 2"))
    db.update("facts", b, {"category": db.q1("SELECT category FROM facts WHERE id = ?", (a,))["category"]})
    keep = db.q1("SELECT * FROM facts WHERE id = ?", (a,))
    other = db.q1("SELECT * FROM facts WHERE id = ?", (b,))
    sid = suggestions.suggest_merge(keep, other, {}, "These look like the same thing.")
    assert db.q1("SELECT 1 FROM facts WHERE id = ?", (b,)), "still there until you approve"
    assert "Merge two facts" in c.get("/profile?tab=facts").text
    suggestions.approve(sid)
    assert not db.q1("SELECT 1 FROM facts WHERE id = ?", (b,))
    assert profile._norm(other["title"]) in db.jload(db.q1("SELECT absorbed FROM facts WHERE id = ?", (a,))["absorbed"], [])


def test_changes_the_app_made_by_itself_are_listed_to_keep_or_undo(fake):
    from career_agent import history, suggestions

    with history.op("seed", source="you"):
        fid = db.insert("facts", {"category": "project", "title": "GearTray", "detail": "A tray app.", "verified": 1,
                                  "created_at": db.now()})
    with history.op("Putting your answers in your career database"):
        db.update("facts", fid, {"detail": "A tray app. Talks to the devices directly.", "updated_at": db.now()})
    assert suggestions.count() == 1
    s = suggestions.waiting()[0]
    assert (s["status"], s["adds"]) == ("applied", "Talks to the devices directly.")
    assert "putting your answers in your career database" in s["why"]
    page = TestClient(app).get("/profile?tab=facts").text
    assert "Already changed" in page and ">Undo<" in page and ">Keep<" in page
    suggestions.refuse(s["id"])
    assert db.q1("SELECT detail FROM facts WHERE id = ?", (fid,))["detail"] == "A tray app.", "put back as it was"
    assert not suggestions.count() and suggestions.listed_before() == 0, "listed once"


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
    assert r.status_code == 303 and "kept+as+true+and+suggested" in r.headers["location"] and "changed" in r.headers["location"]
    text = cvdata.to_markdown(cvdata.base()["data"])
    assert "What is true instead" in text and qs[1]["text"] not in text
    assert sorted(t["kind"] for t in tasks.active(ref="cv-base-check")) == ["check_cv_base", "place_answers"]
    assert review.base_confirmed() == [qs[0]["text"]], "your word counts while the suggestion waits"
    _run("place_answers")
    assert db.scalar("SELECT COUNT(*) FROM facts") == facts, "nothing goes in until you approve it"
    s = db.q1("SELECT * FROM fact_suggestions WHERE status = 'pending'")
    assert s["title"] == "Confirmed fact" and "your base CV" in s["why"] and s["ref"] == "cv-base-check"
    _run("check_cv_base")
    system = next(system for system, user, schema in reversed(fake.calls) if schema is writer.CHECK_SCHEMA)
    assert f"- {qs[0]['text']}" in system, "the check is told the line you said is true"
    assert review.base_confirmed() == [qs[0]["text"]], "kept through the next check"
    assert "Suggested 1 new fact." in c.get("/profile?tab=cv").text
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


def test_a_sentence_you_had_rewritten_for_your_voice_is_left_alone_by_later_reads(fake):
    from career_agent import voice

    settings.set("style_guide", "## Voice in brief\nPlain and a little dry.")
    a = _draft()
    _run("read_voice")
    a = tracker.app_with_job(a["id"])
    assert [n["n"] for n in voice.read_notes(a)["notes"]] == [2]
    TestClient(app).post(f"/applications/{a['id']}/voice-revise", data={"use_2": "1"}, follow_redirects=False)
    assert voice.settled_sentences(tracker.app_with_job(a["id"])) == ["Jeg vil gerne være product owner hos FinPay."]
    writer.revise(a["id"], "Shorter.")  # a revision keeps the record
    assert voice.settled_sentences(tracker.app_with_job(a["id"])) == ["Jeg vil gerne være product owner hos FinPay."]
    voice.settle(a["id"], ["Det lavede jeg selv. Så jeg ville starte med at lære jeres."])  # two sentences: kept as two
    assert voice.settled_sentences(tracker.app_with_job(a["id"]))[1:] == ["Det lavede jeg selv.", "Så jeg ville starte med at lære jeres."]

    # The revision used "something close to" the plainer wording; the fake writer's letter stands in for that here.
    voice.settle(a["id"], ["Jeg søger stillingen som product owner hos FinPay"])
    db.ex("DELETE FROM tasks")
    a = tracker.app_with_job(a["id"])
    db.update("applications", a["id"], {"voice": db.jdump({"read": {**voice.read(a["letter"], "da"), "at": db.now()}})})
    assert voice.read_notes(tracker.app_with_job(a["id"]))["notes"] == [], "a read made before is shown without it"
    db.update("applications", a["id"], {"voice": None})
    writer.run_checks(a["id"])
    _run("read_voice")  # a new read is told, and its note is dropped
    user = next(u for sy, u, sc in reversed(fake.calls) if sc is voice.READ_SCHEMA)
    assert "Sentences [2] were already rewritten the plainer way" in user
    assert voice.read_notes(tracker.app_with_job(a["id"]))["notes"] == []


def test_without_a_style_guide_the_letter_is_not_read_and_is_tried_again_later(fake):
    from career_agent import voice

    a = _draft()
    assert _run("read_voice")["status"] == "done"
    assert not any(sc is voice.READ_SCHEMA for _, _, sc in fake.calls), "nothing to compare with: no model call"
    assert voice.read_notes(tracker.app_with_job(a["id"]))["skipped"] == "no style guide yet"
    db.ex("DELETE FROM tasks")
    writer.run_checks(a["id"])
    assert db.q1("SELECT 1 FROM tasks WHERE kind = 'read_voice'"), "tried again once there may be a guide"
