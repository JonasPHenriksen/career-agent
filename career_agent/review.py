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


def _fact(category: str, title: str, detail: str, job_title: str, about: str | None = None) -> int:
    said = detail or f"Confirmed by you on {date.today().isoformat()}"
    return db.insert("facts", {"category": category, "title": title[:300], "detail": detail[:1500] or None,
                               "evidence": f"Your answer when reviewing {about or 'the application for ' + job_title}: {said}"[:600],
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
        category = "about" if q["kind"] == "assumed" else "project"  # move it into the entry it belongs to when you tidy
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


# ---------------------------------------------------------------------------------------------------------------
# Your base CV against your career database
#
# Every tailored CV starts from the base CV, so a line there that the database doesn't back (or contradicts) is
# flagged again in every application. It is settled once here instead: you say it's true (it becomes a fact) or
# what it should say (the base CV is changed). The career database stays the source of truth.
# ---------------------------------------------------------------------------------------------------------------

def check_base(base_id: int | None = None) -> dict:
    """Fact-check the base CV, line by line, and keep the result with it."""
    from . import cvdata, writer

    base = cvdata.base(base_id)
    if not base:
        return {"skipped": "no base CV"}
    d = writer.check_claims("", cvdata.to_markdown(base["data"]))
    d["checked_at"] = db.now()
    db.update("cv_bases", base["id"], {"claims": db.jdump(d)})
    return {"base_id": base["id"], "claims": d["counts"]}


def base_questions(base: dict | None) -> list[dict]:
    """The base CV's lines the career database doesn't back, as questions for you."""
    claims = (db.jload((base or {}).get("claims"), {}) or {}).get("claims") or []
    return [{"id": f"base:{n}", "kind": "claim", "where": "cv", "text": c["text"], "status": c["status"],
             "ask": ("Your career database doesn't back this line. Is it true?" if c["status"] == "unsupported"
                     else "Part of this line goes beyond, or against, your career database. Is it true as written?"),
             "detail": c.get("note") or "", "yes": "True as written", "no": "Not true: change the CV"}
            for n, c in enumerate(claims) if c.get("status") in FLAGGED]


def _split_period(text: str) -> tuple[str, str]:
    start, _, end = text.replace(" - ", " – ").partition("–")
    return start.strip(), end.strip()


def replace_in_cv(data: dict, old: str, new: str) -> bool:
    """Put `new` where the CV (as data) says `old`, the way the line was shown to you; an empty `new` takes the
    line out. Handles a whole field, a sentence inside a field, a "period · place" line and a skills line.
    Returns False when the line can't be found."""
    old, new = " ".join(old.split()), " ".join(new.split())
    for section in ("work", "education", "projects"):  # "Jul 2024 – Dec 2024 · Horsens" is built from three fields
        for e in data.get(section) or []:
            period = " – ".join(x for x in (e.get("start"), e.get("end")) if x)
            meta = " · ".join(x for x in (period, e.get("location")) if x)
            if old and old in (meta, period):
                when, _, place = new.partition(" · ")
                e["start"], e["end"] = _split_period(when)
                if old == meta and "location" in e and (place or " · " in new):
                    e["location"] = place.strip()
                return True
    for sk in data.get("skills") or []:
        line = f"{sk.get('name')}: {', '.join(sk.get('keywords') or [])}" if sk.get("name") else ", ".join(sk.get("keywords") or [])
        if old == " ".join(line.split()):
            if not new:
                data["skills"].remove(sk)
                return True
            name, sep, words = new.partition(":")
            sk["name"], sk["keywords"] = (name.strip(), words) if sep else (sk.get("name") or "", new)
            sk["keywords"] = [w.strip() for w in sk["keywords"].split(",") if w.strip()]
            return True

    def walk(node) -> bool:
        items = node.items() if isinstance(node, dict) else enumerate(node) if isinstance(node, list) else ()
        for key, value in list(items):
            if isinstance(value, str):
                flat = " ".join(value.split())
                if old and old in flat:
                    changed = " ".join(flat.replace(old, new, 1).split())
                    if not changed and isinstance(node, list):
                        del node[key]
                    else:
                        node[key] = changed
                    return True
            elif isinstance(value, (dict, list)) and walk(value):
                return True
        return False

    return bool(old) and walk(data)


def apply_base_answers(base: dict, answers: dict[str, dict]) -> dict:
    """Your answers about the base CV. True as written: saved as a fact. Not true: the CV says your words instead
    (or the line is taken out when you write nothing). Then the base is checked again."""
    from . import cvdata

    asked = {q["id"]: q for q in base_questions(base)}
    data = base["data"]
    facts = changed = 0
    missed: list[str] = []
    for qid, answer in answers.items():
        q = asked.get(qid)
        verdict, text = (answer.get("verdict") or "").strip(), (answer.get("text") or "").strip()
        if not q or verdict not in ("yes", "no"):
            continue
        if verdict == "yes":
            _fact("project", q["text"], text, "", about="your base CV")
            facts += 1
        elif replace_in_cv(data, q["text"], text):
            changed += 1
        else:
            missed.append(q["text"])
    if changed:
        cvdata.save_base(base["id"], data)
    if facts or changed:
        tasks.enqueue("check_cv_base", {"base_id": base["id"]}, ref="cv-base-check")
    return {"facts": facts, "changed": changed, "missed": missed}


def open_drafts() -> list[int]:
    """Applications you haven't sent, whose CV was made from the base."""
    return [r["id"] for r in db.q("SELECT id FROM applications WHERE status IN ('draft', 'ready') AND cv_data IS NOT NULL ORDER BY id")]
