from __future__ import annotations

import io

from career_agent import (companies, db, exporter, ingest, insights, jobs, privacy, profile, sources, tasks, tracker,
                          voice, writer)
from career_agent.llm import jsonutil

from .conftest import APPLICATION_TEXT, CV_TEXT, JOB_TEXT, approve_waiting


def _import_docs():
    from docx import Document

    d = Document()
    for line in APPLICATION_TEXT.split("\n"):
        d.add_paragraph(line)
    buf = io.BytesIO()
    d.save(buf)
    cv_id, _ = ingest.import_bytes("cv.txt", CV_TEXT.encode())
    app_id, _ = ingest.import_bytes("ansoegning.docx", buf.getvalue())
    return cv_id, app_id


def test_redaction():
    out = privacy.redact("CPR 010190-1234 og 311299 1234, kort 4111 1111 1111 1111, IBAN DK5000400440116243. Tlf 12 34 56 78, 2019-2023.")
    assert "010190" not in out and "4111" not in out and "DK50" not in out
    assert "12 34 56 78" in out and "2019-2023" in out
    assert privacy.redact("Ordrenr 999999-1234") == "Ordrenr 999999-1234"  # not a valid date


def test_json_parse_and_coerce():
    schema = jsonutil.obj(a=jsonutil.s(enum=["x", "y", "other"]), n=jsonutil.i(), items=jsonutil.arr(jsonutil.s()))
    assert jsonutil.parse('```json\n{"a": "x", "n": 2,}\n```') == {"a": "x", "n": 2}
    assert jsonutil.parse("Sure! Here you go: {\"a\": 1} thanks") == {"a": 1}
    assert jsonutil.coerce({"a": "Y", "n": "7.6", "items": "one"}, schema) == {"a": "y", "n": 8, "items": ["one"]}
    assert jsonutil.coerce({"a": "zzz"}, schema)["a"] == "other"


def test_text_extraction_formats(tmp_path):
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("helvetica", size=12)
    pdf.multi_cell(0, 8, "Curriculum vitae. Projektleder hos Novo Nordisk fra 2018 til 2022.")
    path = tmp_path / "cv.pdf"
    path.write_bytes(bytes(pdf.output()))
    assert "Novo Nordisk" in ingest.extract_text(path)
    eml = tmp_path / "mail.eml"
    eml.write_bytes(b"From: Sara <sara@example.com>\nSubject: Status\nContent-Type: text/plain; charset=utf-8\n\n"
                    b"Hej alle, her er status p\xc3\xa5 projektet.\n\nOn Mon, 1 Jan 2024 Bob wrote:\n> old text\n")
    text = ingest.extract_text(eml)
    assert "status på projektet" in text and "old text" not in text


def test_documents_become_facts_with_merging(fake):
    cv_id, app_id = _import_docs()
    profile.analyze_document(cv_id)
    approve_waiting()
    profile.analyze_document(app_id)
    approve_waiting()
    facts = db.q("SELECT * FROM facts")
    experiences = [f for f in facts if f["category"] == "experience"]
    assert len(experiences) == 2, "the Danske Bank role from the application should merge into the CV's"
    assert len([f for f in facts if f["category"] == "skill" and f["title"].lower() == "scrum"]) == 1
    assert any(f["category"] == "about" for f in facts), "what you say about yourself is filed under About me"
    doc = db.q1("SELECT * FROM documents WHERE id = ?", (app_id,))
    assert doc["kind"] == "application" and doc["authored"] == 1 and doc["status"] == "analyzed"
    digest = profile.facts_digest(5000)
    assert "[#" in digest and "Danske Bank" in digest


def test_style_guide_and_voice(fake):
    cv_id, app_id = _import_docs()
    profile.analyze_document(cv_id)
    approve_waiting()
    profile.analyze_document(app_id)
    approve_waiting()
    db.update("documents", app_id, {"outcome": "interview"})
    baseline = voice.compute_baseline()
    assert baseline["da"]["samples"] == 1
    guide = voice.build_style_guide()
    assert "Voice" in guide
    result = voice.check("Jeg brænder for produkter — og jeg er passioneret. " * 5 + "Mere tekst her om mig og mit arbejde.", "da")
    assert any(p["phrase"] == "brænder for" for p in result["phrases"])
    assert any("em dash" in n for n in result["notes"])
    assert voice.exemplars("da")[0]["id"] == app_id


def test_job_to_application_end_to_end(fake):
    cv_id, app_id = _import_docs()
    profile.analyze_document(cv_id)
    approve_waiting()
    profile.analyze_document(app_id)
    approve_waiting()
    job_id = jobs.add_from_text(JOB_TEXT)
    jobs.parse(job_id, use_text=True)
    job = jobs.get(job_id)
    assert job["title"] == "Product Owner" and job["deadline"] == "2030-12-01" and job["company_id"]
    assert db.q1("SELECT * FROM tasks WHERE kind = 'score_job'"), "parsing should queue scoring"

    jobs.score(job_id)
    job = jobs.get(job_id)
    assert job["fit_score"] == 86
    assert 99999 not in job["f"]["matched"][0]["fact_ids"], "invented fact ids are dropped"
    application = db.q1("SELECT * FROM applications WHERE job_id = ?", (job_id,))
    assert application and application["auto_drafted"] == 1, "a strong match is drafted automatically"

    writer.draft(application["id"])
    a = tracker.app_with_job(application["id"])
    assert a["status"] == "draft" and "Danske Bank" in a["letter"] and a["cv"].startswith("# ")
    assert any("Kubernetes" in n for n in db.jload(a["writer_notes"])), "the gap the letter admits is shown to the user"
    claims = db.jload(a["claims"])
    assert claims["counts"].get("unsupported") == 1, claims["counts"]
    assert any("12 udviklere" in c["text"] and c["status"] == "unsupported" for c in claims["claims"])
    assert any("8 udviklere" in c["text"] and c.get("source") and c["status"] == "supported" for c in claims["claims"]), \
        "a sentence the CV backs is found in the documents"
    assert all(99999 not in c["fact_ids"] for c in claims["claims"]), "invented fact ids are dropped"
    assert not any(c["text"].startswith("Kære") for c in claims["claims"]), "greetings aren't claims"
    assert db.jload(a["voice"])["score"] <= 100

    for kind in ("letter", "cv"):
        pdf, name = exporter.render(a, kind, "pdf")
        assert pdf.startswith(b"%PDF") and name.endswith(".pdf") and "Sara Lund" in name
        docx, _ = exporter.render(a, kind, "docx")
        assert docx[:2] == b"PK"

    writer.revise(a["id"], "Make it shorter", "letter")
    assert db.scalar("SELECT COUNT(*) FROM app_versions WHERE application_id = ?", (a["id"],)) >= 2

    tracker.add_event(a["id"], "applied", occurred_at=db.days_ago(20))
    tracker.classify_response(a["id"], "Tak for din ansøgning. Vi har desværre valgt en anden kandidat.")
    a = tracker.app_with_job(a["id"])
    assert a["status"] == "rejected" and a["first_response_at"]
    m = companies.metrics(job["company_id"])
    assert m["applications"] == 1 and m["responded"] == 1 and m["generic_rejections"] == 1 and m["grade"] != "–"

    writer.interview_prep(a["id"])
    writer.followup(a["id"])
    assert insights.funnel()[0]["n"] == 1
    assert insights.response_stats()["sent"] == 1


def test_ghosting_and_followups(fake):
    _import_docs()
    job_id = jobs.add_from_text(JOB_TEXT)
    jobs.parse(job_id, use_text=True)
    app_id = jobs.create_application(job_id)
    tracker.add_event(app_id, "applied", occurred_at=db.days_ago(12))
    assert [f["id"] for f in tracker.followups_due()] == [app_id]
    assert tracker.maintenance()["ghosted"] == 0
    db.update("applications", app_id, {"applied_at": db.days_ago(40)})
    assert tracker.maintenance()["ghosted"] == 1
    assert tracker.app_with_job(app_id)["status"] == "ghosted"
    m = companies.metrics(jobs.get(job_id)["company_id"])
    assert m["ghosted"] == 1 and m["score"] < 50


def test_rss_source_and_triage(fake, monkeypatch):
    rss = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
    <item><title>Product Owner, FinPay A/S</title><link>https://example.com/job/1?utm_source=x</link><description>&lt;p&gt;Great job&lt;/p&gt;</description></item>
    <item><title>Chef, Restaurant</title><link>https://example.com/job/2</link><description>Cook</description></item>
    </channel></rss>"""

    class Resp:
        content = rss
        text = rss.decode()
        url = "https://www.jobindex.dk/jobsoegning.rss?q=po"
        headers = {"content-type": "application/rss+xml"}

    monkeypatch.setattr("career_agent.fetch.get", lambda url, timeout=25: Resp())
    _import_docs()
    profile.analyze_document(1)
    approve_waiting()
    sid = sources.add("Jobindex", "https://www.jobindex.dk/jobsoegning?q=po", kind="rss")
    assert db.q1("SELECT url FROM sources WHERE id = ?", (sid,))["url"].endswith("jobsoegning.rss?q=po")
    result = sources.poll(sid)
    assert result == {"listings": 2, "new": 2, "kept": 1}
    kept = db.q1("SELECT * FROM jobs WHERE url = 'https://example.com/job/1'")
    assert kept and kept["company_name"] == "FinPay A/S" and kept["status"] == "new"
    assert db.q1("SELECT status FROM jobs WHERE url = 'https://example.com/job/2'")["status"] == "filtered"
    assert sources.poll(sid)["new"] == 0, "seen jobs are not added twice"


def test_task_queue_runs_handlers(fake):
    cv_id, _ = _import_docs()
    tid = tasks.enqueue("analyze_document", {"doc_id": cv_id}, ref=f"doc:{cv_id}")
    assert tasks.enqueue("analyze_document", {"doc_id": cv_id}) == tid, "duplicate tasks are merged"
    tasks.run_task(db.q1("SELECT * FROM tasks WHERE id = ?", (tid,)))
    assert db.q1("SELECT status FROM tasks WHERE id = ?", (tid,))["status"] == "done"
    bad = tasks.enqueue("parse_job", {"job_id": 424242})
    tasks.run_task(db.q1("SELECT * FROM tasks WHERE id = ?", (bad,)))
    assert db.q1("SELECT status FROM tasks WHERE id = ?", (bad,))["status"] == "done"  # deleted job: skipped quietly


def test_cv_markdown_parsing():
    blocks = exporter.parse_cv("# Headline\nSummary line\n\n## Erfaring\n### PO — Bank\n2022 – nu\n- Did **things**\n\n## Skills\nA · B")
    kinds = [k for k, _ in blocks]
    assert kinds == ["h1", "para", "h2", "h3", "meta", "bullet", "h2", "para"]
    assert exporter.inline_runs("a **b** c *d*") == [("a ", False, False), ("b", True, False), (" c ", False, False), ("d", False, True)]


def test_contact_header_links_short_and_unbroken():
    p = {"email": "a@b.dk", "phone": "+45 12 34 56 78", "city": "Horsens",
         "linkedin": "https://www.linkedin.com/in/someone/", "website": "github.com/someone"}
    reach, links = exporter.contact_groups(p)
    assert reach[0] == ("a@b.dk", "mailto:a@b.dk") and reach[1] == ("+45 12 34 56 78", "")
    assert links == [("linkedin.com/in/someone", "https://www.linkedin.com/in/someone/"),
                     ("github.com/someone", "https://github.com/someone")], "shown short, still clickable"
    assert "+45 12 34 56 78" in exporter.contact_line(p), "Word may only wrap between items"
    assert exporter.contact_groups({"email": "a@b.dk"}) == [[("a@b.dk", "mailto:a@b.dk")]]


def test_fact_hygiene(fake):
    text = "Projektleder hos Novo Nordisk 2018 - 2021. I dag product owner."
    f = profile._clean_dates({"start_date": "2019", "end_date": "present"}, text)
    assert f == {"start_date": "", "end_date": ""}, "a year that isn't in the document is dropped"
    f = profile._clean_dates({"start_date": "2018-8", "end_date": "nu"}, text)
    assert f == {"start_date": "2018-08", "end_date": "present"}
    assert profile._clean_dates({"start_date": "last year"}, text)["start_date"] == ""
    # vague version first (from a letter), then the CV's precise version wins
    profile.save_facts(None, [{"category": "experience", "title": "Project manager", "organization": "Novo Nordisk",
                               "start_date": "", "end_date": "", "detail": "Ran projects."}])
    approve_waiting()
    profile.save_facts(None, [{"category": "experience", "title": "Projektleder", "organization": "Novo Nordisk A/S",
                               "start_date": "2018-08", "end_date": "2021-12", "detail": "Ledte tre projekter."}],
                       authoritative=True)
    approve_waiting()
    rows = db.q("SELECT * FROM facts WHERE category = 'experience'")
    assert len(rows) == 1 and rows[0]["title"] == "Projektleder" and rows[0]["start_date"] == "2018-08"
    # the same result in two languages, same employer and number: it is stated once, in an entry of that employer
    profile.save_facts(None, [{"category": "achievement", "title": "Reducerede supporthenvendelser med 30 %", "organization": "Danske Bank"}])
    approve_waiting()
    profile.save_facts(None, [{"category": "achievement", "title": "Cut support requests by 30%", "organization": "Danske Bank"}])
    approve_waiting()
    assert db.scalar("SELECT COUNT(*) FROM facts WHERE title LIKE '%30%'") == 1
    assert not db.scalar("SELECT COUNT(*) FROM facts WHERE category NOT IN (%s)" % ", ".join(f"'{c}'" for c in profile.CATEGORY_LABELS))
    assert profile.consolidate() == {"suggested": 0}


def test_pdf_paragraph_detection():
    text = ("Dear hiring team,\nI am applying for the role in your team and I have followed you for a while now and\n"
            "like what you build.\nToday I am the product owner for payments in a bank used by many people and my\n"
            "team works in Scrum.\nBefore that I was a project manager at a large company for three years and it\n"
            "taught me a lot.\nKind regards,\nSara Lund")
    assert len(voice.paragraphs(text)) >= 3


def test_large_profile_keeps_core_facts_within_local_budget(fake):
    import random
    import re

    random.seed(1)
    plan = {"experience": 9, "project": 110, "skill": 120, "education": 3, "certification": 6,
            "language": 3, "utility": 5, "about": 26}
    for cat, n in plan.items():
        for k in range(n):
            db.insert("facts", {"category": cat, "title": f"{cat} {k} " + "lorem ipsum " * random.randint(1, 5),
                                "organization": "Danske Bank", "detail": "Beskrivelse af opgaven. " * random.randint(1, 4),
                                "created_at": db.now()})
    budget = 22_000  # the local model's writing budget
    digest = profile.facts_digest(budget)
    shown = {int(i) for i in re.findall(r"\[#(\d+)\]", digest)}
    for cat in profile.CORE_CATEGORIES:
        ids = {r["id"] for r in db.q("SELECT id FROM facts WHERE category = ?", (cat,))}
        assert ids <= shown, f"every {cat} fact must survive a tight budget"
    assert len(digest) <= budget
    assert "Not shown" in digest
    projects = {r["id"] for r in db.q("SELECT id FROM facts WHERE category = 'project'")}
    assert len(projects & shown) >= 60, "projects are kept before skills and what you say about yourself"
    assert len(profile.facts_digest(10**9)) > len(digest) and "Not shown" not in profile.facts_digest(10**9)


def test_letter_brief_gives_a_goal_not_a_recipe(fake):
    from career_agent import settings
    job = {"id": 1, "title": "Product Owner", "description": JOB_TEXT}
    task = writer._letter_task(job, "da", None)
    assert "at most 400 words" in task and "a ceiling, not a length to aim for" in task and "No paragraph per CV section" in task and "angle" in task
    assert "two to four specific experiences" not in writer.WRITER_SYSTEM, "the old recipe is gone"
    settings.update("writing", {"letter_words": 400})
    assert "about 400 words (between 320 and 460)" in writer._letter_task(job, "da", None), "an explicit setting wins"
    notes = writer._assumed_notes({"assumed": ["You like election systems", " "]})
    assert notes == ["Assumed about you, check it's true: You like election systems"]


def test_old_letters_are_not_shown_next_to_a_folder_style_guide(fake, monkeypatch):
    _import_docs()
    profile.analyze_document(1), profile.analyze_document(2)
    approve_waiting()
    assert "<example" in writer.examples_block("da", None), "without a folder guide, own letters show the voice"
    monkeypatch.setattr("career_agent.knowledge.style_guide", lambda language: "## How she sounds\nPlain.")
    assert writer.examples_block("da", None) == writer.NO_EXAMPLES
    assert "<example" not in writer.writer_system("da")


def test_voice_check_uses_the_style_guides_targets_when_it_has_them(fake, monkeypatch):
    from career_agent import knowledge
    guide = "## How she sounds\nPlain.\n\n## Targets for the voice check\n- avg_sentence: 12–20\n- i_start_pct: 0-33\n\n## Never\nEm dashes."
    monkeypatch.setattr(knowledge, "style_guide", lambda language: guide)
    assert knowledge.voice_targets("da") == {"avg_sentence": (12.0, 20.0), "i_start_pct": (0.0, 33.0)}
    short = " ".join(["Jeg søger jobbet hos jer nu."] * 8)
    v = voice.check(short, "da")
    by_key = {m["key"]: m for m in v["metrics"]}
    assert v["basis"] == "targets" and by_key["avg_sentence"] == {
        "key": "avg_sentence", "label": "Average sentence length (words)", "draft": 6.0, "yours": "12–20", "flag": "below your range"}
    assert by_key["i_start_pct"]["flag"] == "above your range" and by_key["avg_word"]["flag"] == ""
    monkeypatch.setattr(knowledge, "style_guide", lambda language: "## How she sounds\nPlain.")
    assert voice.check(short, "da")["basis"] == "letters"


def test_a_job_far_above_your_level_cannot_score_as_a_strong_match(fake):
    from career_agent import settings
    assert jobs.level_cap("senior", "junior") == 60 and jobs.level_cap("lead", "junior") == 40
    assert jobs.level_cap("senior", "mid") is None and jobs.level_cap("junior", "senior") is None
    assert jobs.level_cap("unknown", "junior") is None and jobs.level_cap("senior", "") is None
    _import_docs()
    profile.analyze_document(1)
    approve_waiting()
    job_id = jobs.add_from_text(JOB_TEXT)
    jobs.parse(job_id, use_text=True)  # the fake reads it as a senior role and scores the fit 86
    settings.update("automation", {"auto_draft": False})
    assert jobs.score(job_id)["score"] == 86, "no level set: no limit"
    settings.update("profile", {"level": "junior"})
    assert jobs.score(job_id) == {"score": 60, "verdict": "stretch"}
    fit = jobs.get(job_id)["f"]
    assert fit["uncapped_score"] == 86 and fit["summary"].startswith("Limited to 60: the ad asks for senior level")
    assert "- Level: Junior" in profile.candidate_block()


def test_voice_check_flags_stale_dates_and_invented_history(fake):
    v = voice.check("Kære Mette\n\nJeg har længe beundret jeres arbejde. Sidste år reducerede vi henvendelserne med 30 %. "
                    "Det var et godt projekt for hele teamet, og vi lærte meget af det undervejs.", "da")
    assert any("sidste år" in n for n in v["notes"]) and any("har længe beundret" in n for n in v["notes"])
    assert "Never invent history" in writer.WRITER_SYSTEM and "sidste år" in writer.WRITER_SYSTEM


def test_fact_check_model_choice(fake):
    from career_agent import settings
    settings.update("llm", {"writing": {"provider": "ollama", "model": "big"}, "check_with": "auto"})
    assert writer.check_tier() == "analysis", "locally the faster model checks"
    settings.update("llm", {"writing": {"provider": "claude_code", "model": "claude-opus-5-5"}})
    assert writer.check_tier() == "writing", "with Claude writing, Claude checks"
    settings.update("llm", {"check_with": "analysis"})
    assert writer.check_tier() == "analysis"


def test_local_model_choice_per_tier():
    from career_agent import llm as L
    models = [{"name": "bge-m3:latest", "size": 1, "details": {"family": "bert"}},
              {"name": "gemma4:26b-mlx", "size": 18}, {"name": "gemma4:31b-mlx", "size": 19}]
    assert L._pick_local(models, L._PREFER_WRITING) == "gemma4:31b-mlx"
    assert L._pick_local(models, L._PREFER_ANALYSIS) == "gemma4:26b-mlx"
    assert L._pick_local([{"name": "bge-m3:latest", "size": 1, "details": {"family": "bert"}}]) == "", "embedding models aren't chat models"
    assert L._pick_local([{"name": "some-new-model:7b", "size": 5}, {"name": "bge-m3", "size": 1}]) == "some-new-model:7b"


def test_example_letters_lose_stale_time_words():
    assert voice.neutral_time_words("Sidste år redesignede vi flowet. I år og last year.") == \
        "Tidligere redesignede vi flowet. For nylig og previously."


def test_gap_note_when_the_letter_skips_the_gap():
    said = {"main_gap": "Udbud", "gap_line": "Jeg har ikke arbejdet med udbud.", "letter": "Kære X\n\nJeg har ikke arbejdet med udbud. Men…"}
    skipped = {"main_gap": "Valg", "gap_line": "Jeg har ikke arbejdet med valg før.", "letter": "Kære X\n\nJeg søger stillingen."}
    assert writer._gap_note(said)[0].startswith("Honest about a gap")
    assert "Jeg har ikke arbejdet med valg før." in writer._gap_note(skipped)[0]
    assert writer._gap_note({"main_gap": "", "gap_line": "", "letter": "x"}) == []


def test_newest_cv_wins_in_either_order(fake):
    def cv(date_):
        return db.insert("documents", {"filename": f"cv {date_}.docx", "sha256": date_, "kind": "cv", "doc_date": date_,
                                       "text": "x" * 40, "word_count": 1, "status": "analyzed", "created_at": db.now()})
    role = {"category": "experience", "title": "Product Owner", "organization": "Danske Bank", "start_date": "2022"}
    for order in (("2023-05", "2025-10"), ("2025-10", "2023-05")):
        db.ex("DELETE FROM facts")
        db.ex("DELETE FROM documents")
        ids = {d: cv(d) for d in order}
        for d in order:
            end = "present" if d == "2023-05" else "2025-06"  # the old CV still says "now"
            profile.save_facts(ids[d], [{**role, "end_date": end, "detail": f"from the {d} CV"}], authoritative=True)
            approve_waiting()
        rows = db.q("SELECT * FROM facts WHERE category = 'experience'")
        assert len(rows) == 1 and rows[0]["end_date"] == "2025-06", f"order {order}: {rows[0]['end_date']}"
        assert rows[0]["document_id"] == ids["2025-10"]


def test_reading_an_ad_keeps_the_dates_its_results_page_gave_when_the_ad_states_none(fake):
    job_id = jobs.add_from_text(JOB_TEXT)
    db.update("jobs", job_id, {"posted_at": "2026-11-16", "deadline": "2026-11-30"})
    jobs.parse(job_id, use_text=True)  # the fake reads no publication date and a deadline of 2030-12-01
    job = jobs.get(job_id)
    assert job["posted_at"] == "2026-11-16", "a date the ad doesn't state is kept"
    assert job["deadline"] == "2030-12-01", "a date the ad states wins"


def test_a_letter_over_the_ceiling_is_cut_once(fake, monkeypatch):
    long_body = " ".join(["ord"] * 430)
    short_body = " ".join(["ord"] * 380)
    letter = {"subject": "Ansøgning", "letter": f"Hej Mette,\n\n{long_body}\n\nMed venlig hilsen\nSara Lund"}
    assert writer.body_words(letter["letter"]) == 430, "greeting and sign-off aren't counted"
    asked = []

    def answer(system, user, schema):
        asked.append(user)
        return {"subject": "Ansøgning", "letter": f"Hej Mette,\n\n{short_body}\n\nMed venlig hilsen\nSara Lund", "main_gap": "",
                "gap_line": "", "used_fact_ids": [], "assumed": [], "angle": "", "notes": []}

    monkeypatch.setattr(fake, "answer", answer)
    notes = writer.fit_ceiling(letter, "SYSTEM", {"title": "Product Owner"}, "da")
    assert notes == ["Shortened from 430 to 380 words to stay under the ceiling of 400."] and writer.body_words(letter["letter"]) == 380
    assert "the ceiling is 400" in asked[0] and len(asked) == 1
    assert writer.fit_ceiling(letter, "SYSTEM", {"title": "Product Owner"}, "da") == [] and len(asked) == 1, "within the ceiling: no model is asked"


def test_the_fact_check_knows_the_town_you_live_in(fake, monkeypatch):
    from career_agent import settings

    settings.update("profile", {"city": "Horsens"})
    seen = []

    def answer(system, user, schema):
        seen.append(system)
        return {"verdicts": [{"n": 1, "kind": "fact", "status": "supported", "fact_ids": [], "problem": ""}]}

    monkeypatch.setattr(fake, "answer", answer)
    writer.check_claims("I live in Horsens, so Aarhus would be the natural office for me.", "")
    assert "The candidate lives in Horsens." in seen[0], "your own setting, so the check doesn't flag it as unbacked"
