"""The agent folder (e.g. ~/Nextcloud/Agents): summaries and style guides in prompts, importing its
facts, never importing it as documents. Plus PowerPoint text and course material."""
from __future__ import annotations

import io
import json
import zipfile

import pytest

from career_agent import db, ingest, knowledge, profile, retrieval, settings, watch, writer

from .conftest import APPLICATION_TEXT, CV_TEXT

PROFILE_MD = """# Sara Lund — career profile

Product owner in fintech with a background in IT management.

## Sources
- Job/CV.txt
"""

AREAS = {
    "work-danske-bank.md": "# Product Owner, Danske Bank (2022–present)\nOwns the payment features of the mobile bank. "
                           "Works with Scrum teams and payments.\n\n## Sources\n- Job/CV.txt\n",
    "education-cbs.md": "# Cand.merc.it, Copenhagen Business School (2016–2018)\nIT management and business "
                        "studies; thesis on digital strategy.\n",
    "free-time.md": "# Free time\nClimbing, board games and baking sourdough bread at the weekend.\n",
}

STYLE_DA = "# Writing style (Danish)\nKorte, varme sætninger. Starter med \"Kære\" og fornavnet."


def _facts():
    cv_quote = "Ansvarlig for mobilbankens betalingsfunktioner med 2 mio. brugere."
    return [
        {"category": "experience", "title": "Product Owner", "organization": "Danske Bank", "start": "2022",
         "end": "present", "detail": "Owns the mobile bank's payment features.", "source": "Job/CV.txt",
         "quote": "Product Owner, Danske Bank, København, 2022 – nu", "work": "solo", "confidential": False},
        {"category": "achievement", "title": "Payment features for 2 million users", "organization": "Danske Bank",
         "start": "", "end": "", "detail": cv_quote, "source": "Job/CV.txt", "quote": cv_quote,
         "work": "unknown", "confidential": True},
        {"category": "skill", "title": "Scrum", "organization": "", "start": "", "end": "", "detail": "Used daily.",
         "source": "Job/CV.txt", "quote": "Scrum, Jira", "work": "unknown", "confidential": False},
        {"category": "made_up_category", "title": "Ignored", "source": "Job/CV.txt", "quote": ""},
        {"category": "personal", "title": "Climbing", "source": "", "quote": "", "detail": "From the CV's interests."},
    ]


SOURCES = [
    {"path": "Job/CV.txt", "kind": "cv", "authorship": "own", "language": "da", "date": "2024-05", "used": True,
     "why": "the newest CV", "summary": "Sara's CV."},
    {"path": "Job/Ansøgning.txt", "kind": "application", "authorship": "own", "language": "da", "date": "2024-03",
     "used": True, "why": "own letter", "summary": "Application to FinPay.", "company": "FinPay",
     "role": "Product Owner", "outcome": "interview"},
    {"path": "Job/Old template letter.txt", "kind": "application", "authorship": "own", "language": "da",
     "date": "2020-02", "used": True, "use_for_style": False, "company": "Lunar", "outcome": "offer"},
    {"path": "Uni/Slides", "kind": "course_material", "authorship": "others", "used": False, "why": "lecture slides"},
]


@pytest.fixture()
def folder(fake, tmp_path, monkeypatch):
    """A Nextcloud-like folder with an Agents/Career Agent part and two source documents."""
    monkeypatch.setattr(retrieval, "embeddings_available", lambda force=False: False)  # keyword ranking only
    nc = tmp_path / "Nextcloud"
    app = nc / "Agents" / "Career Agent"
    (app / "experience").mkdir(parents=True)
    (nc / "Agents" / "about-me.md").write_text("# About Sara\nGeneral notes for any agent.\n", encoding="utf-8")
    (app / "profile.md").write_text(PROFILE_MD, encoding="utf-8")
    for name, text in AREAS.items():
        (app / "experience" / name).write_text(text, encoding="utf-8")
    (app / "writing-style-da.md").write_text(STYLE_DA, encoding="utf-8")
    (app / "facts.jsonl").write_text("\n".join(json.dumps(f, ensure_ascii=False) for f in _facts()) + "\nnot json\n",
                                     encoding="utf-8")
    (app / "sources.jsonl").write_text("\n".join(json.dumps(s, ensure_ascii=False) for s in SOURCES), encoding="utf-8")
    (nc / "Job").mkdir()
    (nc / "Job" / "CV.txt").write_text(CV_TEXT, encoding="utf-8")
    (nc / "Job" / "Ansøgning.txt").write_text(APPLICATION_TEXT, encoding="utf-8")
    (nc / "Job" / "Old template letter.txt").write_text(APPLICATION_TEXT.replace("Mette", "Lunar-teamet"), encoding="utf-8")
    settings.update("knowledge", {"folder": str(nc / "Agents")})
    return nc


def test_load_reads_profile_summaries_and_style(folder):
    data = knowledge.load()
    assert data["profile"].startswith("# Sara Lund")
    assert "Sources" not in data["profile"] and "Job/CV.txt" not in data["profile"]
    assert {a["key"] for a in data["areas"]} == {f"experience/{n}" for n in AREAS}
    assert knowledge.style_guide("da").startswith("# Writing style (Danish)")
    assert knowledge.style_guide("en") == ""
    st = knowledge.status()
    assert st["profile"] and st["has_facts"] and st["style"] == ["da"] and len(st["areas"]) == 3

    # Edits are picked up without restarting.
    (folder / "Agents" / "Career Agent" / "profile.md").write_text("# Sara Lund\nNow a fintech product lead.",
                                                                   encoding="utf-8")
    assert "fintech product lead" in knowledge.load()["profile"]


def test_most_relevant_summaries_come_first(folder):
    top = knowledge.relevant_areas("Product owner for payments and Scrum teams in a bank", 2)
    assert top[0]["key"] == "experience/work-danske-bank.md"
    block = knowledge.context_block("payments Scrum bank", local=True)
    assert block.startswith("# Sara Lund") and block.index("Owns the payment") < block.index("Climbing")
    assert len(knowledge.relevant_areas("payments", 1)) == 1


def test_writer_gets_background_and_the_folders_style_guide(folder):
    settings.set("style_guide", "THE APP'S OWN GUIDE")
    job = {"title": "Product Owner, payments", "summary": "Scrum and payments in a bank.", "description": ""}
    system = writer.writer_system("da", job)
    assert writer.BACKGROUND_INTRO in system
    assert "Owns the payment features of the mobile bank" in system
    assert "Korte, varme sætninger" in system and "THE APP'S OWN GUIDE" not in system
    # English has no guide in the folder, so the app's own is used.
    assert "THE APP'S OWN GUIDE" in writer.writer_system("en", job)
    # The summaries stay out of the fact check: they are never evidence.
    assert writer.BACKGROUND_INTRO not in writer.CHECK_SYSTEM

    settings.update("knowledge", {"use": False})
    off = writer.writer_system("da", job)
    assert writer.BACKGROUND_INTRO not in off and "THE APP'S OWN GUIDE" in off


def test_without_a_folder_the_prompt_is_unchanged(fake):
    system = writer.writer_system("da", {"title": "Product Owner"})
    assert writer.BACKGROUND_INTRO not in system


def test_import_facts_brings_documents_and_facts(folder):
    report = knowledge.import_facts()
    assert report["documents"] == 3 and report["facts_added"] == 4 and report["skipped"] == 1
    old = db.q1("SELECT use_for_style, outcome FROM documents WHERE filename = 'Old template letter.txt'")
    assert (old["use_for_style"], old["outcome"]) == (0, "offer")

    cv = db.q1("SELECT * FROM documents WHERE filename = 'CV.txt'")
    assert (cv["kind"], cv["status"], cv["authored"], cv["doc_date"]) == ("cv", "analyzed", 1, "2024-05")
    letter = db.q1("SELECT * FROM documents WHERE filename = 'Ansøgning.txt'")
    assert (letter["kind"], letter["outcome"], letter["target_company"]) == ("application", "interview", "FinPay")
    assert not db.q("SELECT 1 FROM tasks WHERE kind = 'analyze_document'")  # nothing left for the local model

    po = db.q1("SELECT * FROM facts WHERE category = 'experience'")
    assert po["evidence"] == "Product Owner, Danske Bank, København, 2022 – nu" and po["document_id"] == cv["id"]
    assert (po["start_date"], po["end_date"]) == ("2022", "present") and not po["verified"]
    assert "confidential" in db.q1("SELECT tags FROM facts WHERE category = 'achievement'")["tags"]
    assert db.q1("SELECT document_id FROM facts WHERE title = 'Climbing'")["document_id"] is None

    again = knowledge.import_facts()
    assert again["documents"] == 0 and again["facts_added"] == 0
    assert db.scalar("SELECT COUNT(*) FROM facts") == 4


def test_the_agent_folder_is_never_imported_as_documents(folder, tmp_path):
    result = ingest.import_folder(folder)
    names = {r["filename"] for r in db.q("SELECT filename FROM documents")}
    assert names == {"CV.txt", "Ansøgning.txt", "Old template letter.txt"} and len(result["imported"]) == 3
    assert not any("Agents" in str(p) for p in watch._iter_files(folder))
    refused = ingest.import_folder(folder / "Agents")
    assert not refused["imported"] and "agent folder" in refused["errors"][0][1]

    # A copy somewhere else (a backup, a WebDAV mirror) is recognised by its shape.
    copy = tmp_path / "Backup" / "Agents" / "Career Agent"
    copy.mkdir(parents=True)
    (copy / "profile.md").write_text("# Copy\nA copy of the profile, long enough to import.", encoding="utf-8")
    assert not list(watch._iter_files(tmp_path / "Backup"))


def _pptx(slides: list[str], notes: dict[int, str]) -> bytes:
    def xml(texts):
        paras = "".join(f"<a:p><a:r><a:t>{t}</a:t></a:r></a:p>" for t in texts)
        return f'<p:sld xmlns:a="a" xmlns:p="p"><p:cSld><p:spTree>{paras}</p:spTree></p:cSld></p:sld>'

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, text in enumerate(slides, 1):
            z.writestr(f"ppt/slides/slide{n}.xml", xml(text.split("|")))
        for n, text in notes.items():
            z.writestr(f"ppt/notesSlides/notesSlide{n}.xml", xml([text, str(n)]))
    return buf.getvalue()


def test_powerpoint_text_slide_by_slide(fake, tmp_path):
    p = tmp_path / "deck.pptx"
    p.write_bytes(_pptx(["Project Falcon|Built in Unity &amp; tested", "Results|Grade 12"], {2: "Mention the demo"}))
    text = ingest.extract_text(p)
    assert text.index("[Slide 1]") < text.index("[Slide 2]")
    assert "Built in Unity & tested" in text
    assert "Speaker notes: Mention the demo" in text and "Speaker notes: Mention the demo 2" not in text
    (tmp_path / "broken.pptx").write_bytes(b"not a zip")
    with pytest.raises(ingest.ImportErrorUser):
        ingest.extract_text(tmp_path / "broken.pptx")


def test_course_material_adds_no_facts(fake):
    doc_id, _ = ingest.import_bytes("forelaesning.txt", "Forelæsning 3: kvantemekanik og bølgefunktioner.".encode())
    profile.analyze_document(doc_id)
    doc = db.q1("SELECT kind, authored FROM documents WHERE id = ?", (doc_id,))
    assert (doc["kind"], doc["authored"]) == ("course_material", 0)
    assert db.scalar("SELECT COUNT(*) FROM facts WHERE document_id = ?", (doc_id,)) == 0


def test_profile_card_import_and_settings(folder, tmp_path):
    from fastapi.testclient import TestClient

    from career_agent.web.app import app

    c = TestClient(app)
    r = c.get("/profile?tab=documents")
    assert r.status_code == 200
    assert "Agent folder" in r.text and "Import facts from the folder" in r.text
    assert "3 summaries" in r.text and "style guide: da" in r.text

    r = c.post("/knowledge/import", follow_redirects=False)
    assert r.status_code in (302, 303) and "tab=facts" in r.headers["location"]
    assert db.scalar("SELECT COUNT(*) FROM facts") == 4

    r = c.post("/knowledge/settings", data={"folder": str(tmp_path / "missing"), "use": "1"}, follow_redirects=False)
    assert "err" in r.headers["location"] and settings.get("knowledge")["folder"] == str(folder / "Agents")
    other = tmp_path / "Other agents"
    other.mkdir()
    c.post("/knowledge/settings", data={"folder": str(other)}, follow_redirects=False)
    assert settings.get("knowledge") == {"folder": str(other), "use": False}
    assert "None found" not in c.get("/profile?tab=documents").text  # set, but empty: says so
    assert "Nothing found at the folder set below" in c.get("/profile?tab=documents").text


LETTER_WITH_TABS = """Hej FinPay,

Jeg søger stillingen som product owner, fordi jeg gerne vil arbejde tæt på brugerne af et betalingsprodukt.

Jeg har ikke arbejdet med jeres platform før, men jeg lærer nye systemer hurtigt og i små bidder.

Jeg håber, vi kan tage en snak.

Med venlig hilsen
Sara Lund
Noter til samtalen
""" + " ".join(["Private notes about salary expectations and the case task that must never be used as an example."] * 6)


def test_only_the_letter_counts_as_the_letter(folder):
    from career_agent import voice

    letter = voice.letter_part(LETTER_WITH_TABS)
    assert letter.endswith("Med venlig hilsen\nSara Lund") and "salary" not in letter
    short_ps = "Hej,\n\nTekst her.\n\nMed venlig hilsen\nSara Lund\nP.S. Jeg kan starte med det samme."
    assert voice.letter_part(short_ps) == short_ps
    assert voice.letter_part("No sign-off at all, just text.") == "No sign-off at all, just text."

    (folder / "Job" / "Ansøgning.txt").write_text(LETTER_WITH_TABS, encoding="utf-8")
    knowledge.import_facts()
    doc = db.q1("SELECT id, text FROM documents WHERE filename = 'Ansøgning.txt'")
    assert "salary" not in doc["text"] and doc["text"].endswith("Sara Lund")
    assert not db.q("SELECT 1 FROM chunks WHERE document_id = ? AND text LIKE '%salary%'", (doc["id"],))


def test_word_paragraphs_are_counted_one_per_line(fake):
    from career_agent import voice

    para = "Dette er et langt afsnit med mange ord, der fylder en hel linje i et Word-dokument og fortsætter lidt endnu."
    text = "Hej Sara,\n" + "\n".join([para + " " + para] * 4) + "\nMed venlig hilsen\nSara Lund"
    # four paragraphs plus "Med venlig hilsen"; the greeting and the name have under three words
    assert len(voice.paragraphs(text)) == 5
    assert "nyuddannet" in voice.RISKY_TIME_WORDS and "just graduated" in voice.RISKY_TIME_WORDS


def test_a_letter_does_not_overwrite_what_the_cv_says(folder):
    cv_id = ingest.import_path(folder / "Job" / "CV.txt")[0]
    db.update("documents", cv_id, {"kind": "cv", "status": "analyzed"})
    letter_id = ingest.import_path(folder / "Job" / "Ansøgning.txt")[0]
    db.update("documents", letter_id, {"kind": "application", "status": "analyzed"})
    role = {"category": "experience", "title": "Product Owner", "organization": "Danske Bank", "start_date": "2022",
            "end_date": "present", "detail": "Owns the payment features.", "evidence": "Product Owner, Danske Bank"}
    profile.save_facts(cv_id, [role], authoritative=True)
    longer = {**role, "detail": "A letter that says a lot more about the role, in many more words than the CV does."}
    profile.save_facts(letter_id, [longer], authoritative=False)
    assert db.q1("SELECT detail FROM facts WHERE category = 'experience'")["detail"] == "Owns the payment features."
