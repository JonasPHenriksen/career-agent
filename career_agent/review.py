"""Reviewing a draft: what to ask you about it, and what happens with your answers.

A draft raises questions only you can answer: is the gap the writer named real, is what it assumed about you
true, is a sentence the fact check couldn't back actually true? Your answers become facts in your career database
(so the next draft knows), and the draft is revised to match them.
"""
from __future__ import annotations

from datetime import date
from html import escape

from . import db, tasks

FLAGGED = ("partly", "unsupported")


def questions(app: dict) -> list[dict]:
    """The open questions about a draft, most important first: the gap, what was assumed, what isn't backed."""
    review = db.jload(app.get("review"), {}) or {}
    claims = (db.jload(app.get("claims"), {}) or {}).get("claims") or []
    out = []
    if review.get("main_gap"):
        told = ("The letter says so plainly." if review.get("gap_in_letter") else "The letter doesn't mention it.")
        out.append({"id": "gap", "kind": "gap", "where": "letter", "text": review["main_gap"],
                    "ask": "The ad asks for this and your career database doesn't show it. Do you have it?",
                    "detail": f"{told}" + (f" Suggested sentence: “{review['gap_line']}”" if review.get("gap_line") and not review.get("gap_in_letter") else ""),
                    "yes": "I do have this", "no": "Right, it's new to me"})
    for n, text in enumerate(review.get("assumed") or []):
        out.append({"id": f"assumed:{n}", "kind": "assumed", "where": "letter", "text": text,
                    "ask": "The writer assumed this about you. Is it true?", "detail": "",
                    "yes": "True", "no": "Not true"})
    for n, c in enumerate(claims):
        if c.get("status") in FLAGGED:
            out.append({"id": f"claim:{n}", "kind": "claim", "where": c.get("where") or "letter", "text": c["text"],
                        "status": c["status"],
                        "ask": ("Your career database doesn't back this. Is it true?" if c["status"] == "unsupported"
                                else "Part of this goes beyond your career database. Is it true as written?"),
                        "detail": c.get("note") or "", "yes": "True as written", "no": "Not true"})
    return out


def marked(text: str, app: dict, where: str = "letter") -> str:
    """The text as HTML with the sentences the fact check flagged marked, each with its reason."""
    claims = (db.jload(app.get("claims"), {}) or {}).get("claims") or []
    html = escape(text or "")
    for c in claims:
        if c.get("status") not in FLAGGED or (c.get("where") or "letter") != where:
            continue
        sentence = escape(c["text"])
        if sentence and sentence in html:
            html = html.replace(sentence, f'<mark class="flag {c["status"]}" title="{escape(c.get("note") or "", quote=True)}">'
                                          f'{sentence}</mark>', 1)
    return "".join(f"<p>{p.replace(chr(10), '<br>')}</p>" for p in html.split("\n\n") if p.strip())


def _fact(category: str, title: str, detail: str, job_title: str) -> int:
    said = detail or f"Confirmed by you on {date.today().isoformat()}"
    return db.insert("facts", {"category": category, "title": title[:300], "detail": detail[:1500] or None,
                               "evidence": f"Your answer when reviewing the application for {job_title}: {said}"[:600],
                               "source": "manual", "verified": 1, "tags": "your answer", "created_at": db.now()})


def apply_answers(app: dict, answers: dict[str, dict], model: dict | None = None) -> dict:
    """Save what you confirmed or explained as facts, and revise the draft where you said something isn't true or
    told it more. `answers` is {question id: {"verdict": "yes" | "no" | "", "text": your words}}."""
    asked = {q["id"]: q for q in questions(app)}
    job_title = app.get("job_title") or "this job"
    facts, instructions, parts = 0, [], set()
    for qid, answer in answers.items():
        q = asked.get(qid)
        verdict, text = (answer.get("verdict") or "").strip(), (answer.get("text") or "").strip()
        if not q or not (verdict or text):
            continue
        if q["kind"] == "gap":
            if verdict == "yes" or (text and verdict != "no"):
                _fact("skill", q["text"], text, job_title)
                facts += 1
                instructions.append(f"The candidate does have “{q['text']}”" + (f": {text}" if text else "")
                                    + ". It is in the career database now. Don't present it as a gap; use it where it helps.")
            else:
                instructions.append(f"“{q['text']}” really is new to the candidate" + (f" ({text})" if text else "")
                                    + ". Say so in one plain sentence next to the closest real experience, if the letter doesn't already.")
            parts.add("letter")
            continue
        category = "value" if q["kind"] == "assumed" else "achievement"
        if verdict == "no":
            if text:  # what is true instead is worth keeping
                _fact(category, text, "", job_title)
                facts += 1
            instructions.append(f"This is not true, so take it out or put the truth in its place: “{q['text']}”"
                                + (f". What is true: {text}" if text else "."))
            parts.add(q["where"])
        else:  # true, with or without more detail
            _fact(category, q["text"], text, job_title)
            facts += 1
            if text:
                instructions.append(f"About “{q['text']}” the candidate adds: {text}. It is in the career database now; use it if it makes the text better.")
                parts.add(q["where"])
    extra = model or {}
    if instructions:
        part = "both" if parts >= {"letter", "cv"} else ("cv" if parts == {"cv"} else "letter")
        tasks.enqueue("revise_application", {"application_id": app["id"], "part": part, **extra,
                                             "instructions": "The candidate answered questions about this draft. Change only what "
                                                             "their answers require:\n- " + "\n- ".join(instructions)},
                      ref=f"app:{app['id']}", dedupe=False)
    elif facts:  # only confirmations: the text stays, the fact check now has the facts to find
        tasks.enqueue("check_application", {"application_id": app["id"], **extra}, ref=f"app:{app['id']}")
    return {"facts": facts, "revised": bool(instructions)}
