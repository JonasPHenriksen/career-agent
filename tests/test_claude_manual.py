"""The Claude.ai copy/paste flow: the prompt, reading the pasted answer back, and the pages."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from career_agent import db, ingest, jobs, profile, writer
from career_agent.web.app import app

from .conftest import CV_TEXT, JOB_TEXT

ANSWER = """===SUBJECT===
Ansøgning: Product Owner hos FinPay
===GAP===
none
===LETTER===
Kære Mette Holm

Jeg søger stillingen som product owner hos FinPay. I Danske Bank har jeg ansvaret for betalingsfunktionerne i mobilbanken, og vi fik supporthenvendelserne ned med 30 % [#3].

Med venlig hilsen
Sara Lund
===CV===
```markdown
# Product Owner, betalinger
Erfaren product owner.

## Erfaring
### Product Owner — Danske Bank
2022 – nu · København
- Reducerede supporthenvendelser med 30 %
```
===NOTES===
- I kept it short, like your own letters.
- Check the greeting name.
"""


def _app(fake):
    doc_id, _ = ingest.import_bytes("cv.txt", CV_TEXT.encode())
    profile.analyze_document(doc_id)
    job_id = jobs.add_from_text(JOB_TEXT)
    jobs.parse(job_id, use_text=True)
    return job_id, jobs.create_application(job_id, draft=False)


def test_prompt_has_everything_claude_needs(fake):
    _, app_id = _app(fake)
    assert db.q1("SELECT status FROM applications WHERE id = ?", (app_id,))["status"] == "draft"
    assert not db.q1("SELECT 1 FROM tasks WHERE kind = 'draft_application'"), "no local draft is started"
    p = writer.manual_prompt(app_id)
    for needle in ("Danske Bank", "Product Owner", "FinPay", "===LETTER===", "===CV===", "sidste år", "must-have",
                   "TRUTH RULES", "## Erfaring"):
        assert needle in p, needle
    assert "010190" not in writer.manual_prompt(app_id)  # the redaction runs (nothing to redact here, but no crash)


@pytest.mark.parametrize("variant", [
    ANSWER,
    ANSWER.replace("===LETTER===", "**===LETTER===**").replace("===CV===", "### ===CV==="),
    "```\n" + ANSWER + "\n```",
    '{"subject": "S", "letter": "Kære Mette\\n\\nTekst.", "cv_markdown": "# PO", "notes": ["n"]}',
])
def test_reads_the_answer_back(variant):
    d = writer.parse_manual_answer(variant)
    assert d["letter"].startswith("Kære Mette")
    assert d["cv"].startswith("# ")
    assert d["main_gap"] == ""


def test_rejects_an_answer_without_sections():
    with pytest.raises(ValueError):
        writer.parse_manual_answer("Here is your letter: Kære Mette ...")


def test_pages_and_import(fake):
    job_id, _ = _app(fake)
    db.ex("DELETE FROM applications")
    c = TestClient(app)
    r = c.post(f"/jobs/{job_id}/write", data={"mode": "claude"}, follow_redirects=False)
    assert r.status_code == 303 and "?tab=claude" in r.headers["location"]  # followed by the undo link
    app_id = int(r.headers["location"].split("/applications/")[1].split("?")[0])
    page = c.get(f"/applications/{app_id}?tab=claude").text
    assert "Copy prompt" in page and "===SUBJECT===" in page and "claude.ai/new" in page
    assert "No draft yet" in c.get(f"/applications/{app_id}?tab=letter").text
    r = c.post(f"/applications/{app_id}/manual-import", data={"text": ANSWER}, follow_redirects=False)
    assert r.status_code == 303 and "tab=letter" in r.headers["location"]
    a = db.q1("SELECT * FROM applications WHERE id = ?", (app_id,))
    assert a["subject"] == "Ansøgning: Product Owner hos FinPay" and "[#3]" not in a["letter"]
    assert a["cv"].startswith("# Product Owner") and "```" not in a["cv"]
    assert any("Claude.ai" in n for n in db.jload(a["writer_notes"]))
    assert db.q1("SELECT 1 FROM tasks WHERE kind = 'check_application'"), "the fact check is queued"
    r = c.post(f"/applications/{app_id}/manual-import", data={"text": "no sections"}, follow_redirects=False)
    assert "err=" in r.headers["location"]
