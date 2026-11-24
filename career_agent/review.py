"""Reviewing a draft: what to ask you about it, and what happens with your answers.

A draft raises questions only you can answer: is the gap the writer named real, is what it assumed about you
true, is a sentence the fact check couldn't back actually true? What you confirm about your career is suggested for
your career database, where you approve it before later drafts use it (suggestions.py): as an addition to the fact
it's part of, as a new fact, or not at all when the database already says it or it isn't about your career.
Corrections only change the draft, and what the writer assumed about your motives is confirmed for that application
alone: a line written for one employer isn't evidence of who you are. A sentence you said is true, and a gap you've
answered, aren't asked about again in that application, even when a revision rewords them, and count as true for
its fact check while the suggestion waits for you.
"""
from __future__ import annotations

import re
from html import escape

from . import db, profile, tasks
from .llm import jsonutil as J

FLAGGED = ("partly", "unsupported")


def _same(a: str, b: str) -> bool:
    return " ".join((a or "").lower().split()) == " ".join((b or "").lower().split())


_FILLER = {"and", "or", "the", "a", "an", "of", "in", "with", "for", "og", "eller", "i", "med", "af", "en", "et",
           "domain", "experience", "knowledge", "erfaring", "viden"}


def _same_gap(a: str, b: str) -> bool:
    """The same gap, though each revision's writer names it a little differently: "XBRL, annual reports and
    accounting" became "XBRL, annual reports and accounting domain" and was asked about again."""
    x, y = ({w for w in re.findall(r"\w+", (s or "").lower()) if w not in _FILLER} for s in (a, b))
    if not x or not y:
        return _same(a, b)
    return len(x & y) / min(len(x), len(y)) >= 0.6


def answered_gap(review: dict) -> dict | None:
    """Your answer to the gap this draft names, when you've answered it before in this application."""
    gap = review.get("main_gap") or ""
    return next((g for g in review.get("gap_answers") or [] if gap and _same_gap(gap, g.get("text") or "")), None)


def questions(app: dict) -> list[dict]:
    """The open questions about a draft, most important first: the gap, what was assumed, what isn't backed."""
    review = db.jload(app.get("review"), {}) or {}
    claims = (db.jload(app.get("claims"), {}) or {}).get("claims") or []
    confirmed = review.get("confirmed") or []
    out = []
    if review.get("main_gap") and not answered_gap(review):
        told = ("The letter says so plainly." if review.get("gap_in_letter") else "The letter doesn't mention it.")
        out.append({"id": "gap", "kind": "gap", "where": "letter", "text": review["main_gap"],
                    "ask": "The ad asks for this and your career database doesn't show it. Do you have it?",
                    "detail": f"{told}" + (f" Suggested sentence: “{review['gap_line']}”" if review.get("gap_line") and not review.get("gap_in_letter") else ""),
                    "yes": "I do have this", "no": "Right, it's new to me"})
    for n, text in enumerate(review.get("assumed") or []):
        if any(_same(text, c) for c in confirmed):
            continue
        out.append({"id": f"assumed:{n}", "kind": "assumed", "where": "letter", "text": text,
                    "ask": "The writer assumed this about you. Is it true?",
                    "detail": "Your answer counts for this application only; it isn't saved in your career database.",
                    "yes": "True", "no": "Not true"})
    for n, c in enumerate(claims):
        if c.get("status") in FLAGGED and not any(_same(c["text"], x) for x in confirmed):
            out.append({"id": f"claim:{n}", "kind": "claim", "where": c.get("where") or "letter", "text": c["text"],
                        "status": c["status"], "fact_ids": c.get("fact_ids") or [],
                        "ask": ("Your career database doesn't back this. Is it true?" if c["status"] == "unsupported"
                                else "Part of this goes beyond your career database. Is it true as written?"),
                        "detail": c.get("note") or "", "yes": "True as written", "no": "Not true"})
    return out


def marked(text: str, app: dict, where: str = "letter") -> str:
    """The text as HTML with the sentences the fact check flagged marked, each with its reason; in the letter also
    the sentences that don't sound like you (dotted), each with a plainer version."""
    from . import voice

    claims = (db.jload(app.get("claims"), {}) or {}).get("claims") or []
    confirmed = (db.jload(app.get("review"), {}) or {}).get("confirmed") or []
    html = escape(text or "")
    read = voice.read_notes(app) if where == "letter" else None
    for note in (read or {}).get("notes") or []:
        sentence = escape(note["text"])
        tip = escape(f"{note['problem']}. Plainer: {note['plainer']}", quote=True)
        if sentence and sentence in html:
            html = html.replace(sentence, f'<mark class="flag voice" title="{tip}">{sentence}</mark>', 1)
    for c in claims:
        if c.get("status") not in FLAGGED or (c.get("where") or "letter") != where or any(_same(c["text"], x) for x in confirmed):
            continue
        sentence = escape(c["text"])
        if sentence and sentence in html:
            html = html.replace(sentence, f'<mark class="flag {c["status"]}" title="{escape(c.get("note") or "", quote=True)}">'
                                          f'{sentence}</mark>', 1)
    return "".join(f"<p>{p.replace(chr(10), '<br>')}</p>" for p in html.split("\n\n") if p.strip())


# ---------------------------------------------------------------------------------------------------------------
# Suggesting what you confirmed for your career database
#
# A confirmed sentence is the letter's (or the CV's) wording, written for one job. Saved as it stands it became a
# "project" with no place, in Danish and the first person, next to the fact that already said it. So a model reads
# it against the facts the fact check linked it to and the rest of the database, and suggests only what's new: an
# addition to the fact it's part of, or a new fact. Nothing goes in until you approve it under Pending changes.
# ---------------------------------------------------------------------------------------------------------------

PLACE_SYSTEM = """You keep a candidate's career database: the facts their job applications may use. The candidate \
has just confirmed some things while reviewing an application. Decide for each one where it goes.

For each numbered item choose one action:
- known: the database already says all of it (maybe in other words), including what the fact check found missing. \
Give that fact's id in fact_id.
- add_to: it adds a detail to one fact that's already there (usually a job, project or education the fact check \
linked it to). Give that fact's id in fact_id, and in addition only what the fact doesn't say yet (usually what the \
fact check found missing).
- new: it's about the candidate's career (experience, a project, a skill, education, a certificate or a language) \
and no fact covers it. Fill in the new fact. Use the category "about" only for how the candidate works or what they \
value when their own words below the item say so; a line from the letter alone is written for one employer.
- skip: it isn't a fact about the candidate's career: when they can start or their notice period, salary, why they \
want this one job or what they'd do first there, an opinion about the employer, a motive or value only the letter \
states, a greeting or a closing line, or a remark that only corrects the wording.

Rules:
- Use only what the item and the candidate's own words say. Never add employers, titles, dates, numbers or tools.
- Write in English, the way the database does: no first person, no "I", no sentence copied from the letter.
- A title is a few words naming the thing (like the database's titles), never a whole sentence.
- organization is the employer, school or place as the database names it; empty when there's none.
- An item may say what the fact check found the database doesn't say. The candidate has now confirmed that part, so \
choose known only when the database really says it. Otherwise add it (add_to, or new) when it's about their career: \
a scope, how often, a tool, a document type, a number or a date. Leave it out when it's only wording or a \
characterisation.
- When the fact check found nothing missing and you're in doubt between known and add_to, choose known. When in \
doubt between new and skip, choose skip."""

PLACE_SCHEMA = J.obj(results=J.arr(J.obj(
    n=J.i("The item's number"),
    action=J.s("Where it goes", enum=["known", "add_to", "new", "skip"]),
    fact_id=J.i("known or add_to: the fact's id. Otherwise 0"),
    addition=J.s("add_to: one or two sentences in English with only what the fact doesn't say yet. Otherwise empty"),
    category=J.s("new: one of " + ", ".join(c for c, _ in profile.CATEGORIES) + ". Otherwise empty"),
    title=J.s("new: a short title in English. Otherwise empty"),
    organization=J.s("new: the employer, school or place, as the database names it; empty if none"),
    start_date=J.s("new: YYYY or YYYY-MM when the item says so; otherwise empty"),
    end_date=J.s("new: YYYY, YYYY-MM or present when the item says so; otherwise empty"),
    detail=J.s("new: one to three sentences in English with what the candidate confirmed. Otherwise empty"),
    why=J.s("A few words in English: why this choice"),
)))


def _item(q: dict, said: str) -> dict:
    """A confirmed question, as it goes to `place`. A flagged sentence carries what the fact check found missing:
    without it, `place` saw the core of the sentence in the database, called it known and saved nothing, so the next
    check flagged the same words and the same question came back."""
    return {"kind": q["kind"], "text": q["text"], "said": said, "fact_ids": q.get("fact_ids") or [],
            "missing": (q.get("detail") or "") if q.get("status") in FLAGGED else ""}


def _linked(ids: list[int]) -> str:
    rows = [db.q1("SELECT * FROM facts WHERE id = ?", (i,)) for i in dict.fromkeys(ids)]
    return "\n".join(f"[#{f['id']}] {f['title']}" + (f" ({f['organization']})" if f.get("organization") else "")
                     + (f": {f['detail']}" if f.get("detail") else "") for f in rows if f)


def place(items: list[dict], about: str, ref: str | None = None) -> dict:
    """Suggest what you confirmed for your career database (see PLACE_SYSTEM): nothing changes there until you
    approve it under Pending changes. Returns what was suggested, with a line saying so for the page."""
    from . import llm, suggestions

    if not items:
        return {"added_to": [], "new": [], "known": 0, "skipped": 0, "said": ""}
    blocks = []
    for n, it in enumerate(items, 1):
        what = {"gap": "The job ad asks for this and the candidate says they have it",
                "claim": "The candidate confirmed this sentence from the application is true",
                "cv_line": "The candidate confirmed this line from their own CV is true"}.get(it["kind"], "The candidate confirmed this")
        block = f"[{n}] {what}:\n“{it['text']}”"
        if it.get("said"):
            block += f"\nThe candidate's own words about it: “{it['said']}”"
        if it.get("missing"):
            block += f"\nWhat the fact check found the database doesn't say: {it['missing']}"
        linked = _linked(it.get("fact_ids") or [])
        if linked:
            block += f"\nFacts the fact check linked it to:\n{linked}"
        blocks.append(block)
    query = "\n".join(f"{it['text']} {it.get('said') or ''}" for it in items)
    system = PLACE_SYSTEM + "\n\nCAREER DATABASE:\n" + profile.facts_digest(profile.limits()["facts_chars_analysis"], query=query)
    d = llm.run("analysis", "place_answers", system, "ITEMS:\n\n" + "\n\n".join(blocks), PLACE_SCHEMA,
                max_tokens=4000, effort="medium").data
    by_n = {r["n"]: r for r in d["results"] if 1 <= r["n"] <= len(items)}
    categories = {c for c, _ in profile.CATEGORIES}
    added_to: list[str] = []
    new: list[str] = []
    known = skipped = 0
    for n, it in enumerate(items, 1):
        r = by_n.get(n) or {"action": "skip"}
        evidence = (f"Your answer when reviewing {about}: “{it['text']}”" + (f" ({it['said']})" if it.get("said") else ""))[:600]
        target = db.q1("SELECT * FROM facts WHERE id = ?", (r.get("fact_id") or 0,)) if r["action"] in ("known", "add_to") else None
        if r["action"] == "add_to" and target and (r.get("addition") or "").strip():
            detail = " ".join(x for x in ((target.get("detail") or "").strip(), r["addition"].strip()) if x)
            if suggestions.suggest_edit(target, {"detail": detail}, evidence, "answers", ref=ref):
                added_to.append(target["title"])
            else:
                known += 1  # the same addition is already waiting, or you refused it
        elif r["action"] == "known" and target:
            known += 1
        elif r["action"] == "new" and (r.get("title") or "").strip():
            fact = {"category": r["category"] if r.get("category") in categories else "project",
                    "title": r["title"].strip()[:300], "organization": (r.get("organization") or "").strip()[:200],
                    "start_date": (r.get("start_date") or "").strip()[:10], "end_date": (r.get("end_date") or "").strip()[:10],
                    "detail": (r.get("detail") or "").strip()[:1500], "evidence": evidence, "tags": "your answer"}
            if suggestions.suggest_new(fact, evidence, "answers", ref=ref):
                new.append(r["title"].strip())
            else:
                known += 1
        else:
            skipped += 1
    return {"added_to": added_to, "new": new, "known": known, "skipped": skipped,
            "said": placed_line(added_to, new, known, skipped)}


def placed_line(added_to: list[str], new: list[str], known: int, skipped: int) -> str:
    parts = [f"suggested an addition to “{t}”" for t in added_to]
    if new:
        parts.append(f"suggested {len(new)} new fact{'s' if len(new) != 1 else ''}")
    if known:
        parts.append(f"{known} your career database already had")
    if skipped:
        parts.append(f"{skipped} not about your career, so not saved")
    line = "; ".join(parts)
    if not line:
        return ""
    line = line[:1].upper() + line[1:] + "."
    if added_to or new:
        line += " Nothing changes until you approve it under Profile → Career database → Pending changes."
    return line


def place_task(payload: dict) -> dict:
    """The queued step: suggest the answers for your career database. Tasks queued before suggestions existed carry
    the step that waited for them, which is started here."""
    result = place(payload.get("items") or [], payload.get("about") or "an application", payload.get("ref"))
    then = payload.get("then")
    if then:
        tasks.enqueue(then["kind"], then["payload"], ref=then.get("ref"), dedupe=then["kind"] != "revise_application")
    return result


def last_placed(ref: str) -> str:
    """What was suggested from your latest answers, once that's done (shown where you answered)."""
    import json

    row = db.q1("SELECT * FROM tasks WHERE kind = 'place_answers' AND ref = ? AND finished_at >= ? ORDER BY id DESC LIMIT 1",
                (ref, db.days_ago(2)))
    if not row or row["status"] != "done" or not row.get("progress"):
        return ""
    try:
        return json.loads(row["progress"]).get("said") or ""
    except ValueError:
        return ""


def _queue(items: list[dict], about: str, ref: str, then: tuple[str, dict] | None) -> None:
    """Suggest the answers for your career database, and start the revision or the check. Neither waits for the
    other: the database doesn't change until you approve, and the answers reach the draft and its check directly."""
    if items:
        tasks.enqueue("place_answers", {"items": items, "about": about, "ref": ref}, ref=ref, dedupe=False)
    if then:
        tasks.enqueue(then[0], then[1], ref=ref, dedupe=then[0] != "revise_application")


def apply_answers(app: dict, answers: dict[str, dict], model: dict | None = None) -> dict:
    """What you confirmed is placed in your career database, and the draft is revised where you said something
    isn't true or told it more. `answers` is {question id: {"verdict": "yes" | "no" | "", "text": your words}}."""
    asked = {q["id"]: q for q in questions(app)}
    job_title = app.get("job_title") or "this job"
    place_items: list[dict] = []
    confirmed: list[str] = []
    settled: list[str] = []  # sentences you said are true: placed, and not asked about again in this application
    gaps: list[dict] = []  # what you said about the gap: the next draft names it again, but it isn't asked again
    instructions: list[str] = []
    parts: set[str] = set()
    for qid, answer in answers.items():
        q = asked.get(qid)
        verdict, text = (answer.get("verdict") or "").strip(), (answer.get("text") or "").strip()
        if not q or not (verdict or text):
            continue
        if q["kind"] == "gap":
            has = verdict == "yes" or bool(text and verdict != "no")
            gaps.append({"text": q["text"], "has": has})
            if has:
                place_items.append(_item(q, text))
                instructions.append(f"The candidate does have “{q['text']}”" + (f": {text}" if text else "")
                                    + ". Don't present it as a gap; use it where it helps.")
            else:
                instructions.append(f"“{q['text']}” really is new to the candidate" + (f" ({text})" if text else "")
                                    + ". Say so in one plain sentence next to the closest real experience, if the letter doesn't already.")
            parts.add("letter")
            continue
        if verdict == "no":  # a correction changes the draft; it isn't a fact about you
            instructions.append(f"This is not true, so take it out or put the truth in its place: “{q['text']}”"
                                + (f". What is true: {text}" if text else "."))
            parts.add(q["where"])
            continue
        if q["kind"] == "assumed":  # true for this application; one letter's motive isn't put in About me
            confirmed.append(q["text"])
        else:
            place_items.append(_item(q, text))
            settled.append(q["text"])
        if text:
            instructions.append(f"About “{q['text']}” the candidate adds: {text}. Use it if it makes the text better.")
            parts.add(q["where"])
    if confirmed or settled or gaps:  # the writer and the fact check are told, so a revision doesn't raise them again
        review = db.jload(app.get("review"), {}) or {}
        if confirmed or settled:
            review["confirmed"] = list(dict.fromkeys([*(review.get("confirmed") or []), *confirmed, *settled]))
        if gaps:
            kept = [g for g in review.get("gap_answers") or [] if not any(_same_gap(g["text"], n["text"]) for n in gaps)]
            review["gap_answers"] = [*kept, *gaps]
        db.update("applications", app["id"], {"review": db.jdump(review)})
    then = None
    if instructions:
        part = "both" if parts >= {"letter", "cv"} else ("cv" if parts == {"cv"} else "letter")
        then = ("revise_application", {"application_id": app["id"], "part": part, **(model or {}),
                                       "instructions": "The candidate answered questions about this draft. Change only what "
                                                       "their answers require:\n- " + "\n- ".join(instructions)})
    elif place_items:  # only confirmations: the text stays, the fact check then finds them in the database
        then = ("check_application", {"application_id": app["id"], **(model or {})})
    _queue(place_items, f"the application for {job_title}", f"app:{app['id']}", then)
    return {"placing": len(place_items), "confirmed": len(confirmed), "revised": bool(instructions)}


# ---------------------------------------------------------------------------------------------------------------
# Your base CV against your career database
#
# Every tailored CV starts from the base CV, so a line there that the database doesn't back (or contradicts) is
# flagged again in every application. It is settled once here instead: you say it's true (it's suggested as a fact,
# and counts as true meanwhile) or what it should say (the base CV is changed). The career database stays the
# source of truth.
# ---------------------------------------------------------------------------------------------------------------

def base_confirmed() -> list[str]:
    """Base CV lines you said are true: your own word (like where you live), so every fact check counts them as
    backed, also while what they add to your career database waits for your approval."""
    out: list[str] = []
    for b in db.q("SELECT claims FROM cv_bases"):
        out += (db.jload(b["claims"], {}) or {}).get("confirmed") or []
    return list(dict.fromkeys(out))


def check_base(base_id: int | None = None, full: bool = False) -> dict:
    """Fact-check the base CV, line by line, and keep the result with it. Only lines that changed since the last
    check are read again (see writer.check_claims), unless `full`."""
    from . import cvdata, writer

    base = cvdata.base(base_id)
    if not base:
        return {"skipped": "no base CV"}
    before = db.jload(base.get("claims"), {}) or {}
    confirmed = before.get("confirmed") or []
    d = writer.check_claims("", cvdata.to_markdown(base["data"]), confirmed=confirmed, before=None if full else before)
    d["checked_at"] = db.now()
    if confirmed:
        d["confirmed"] = confirmed
    db.update("cv_bases", base["id"], {"claims": db.jdump(d)})
    return {"base_id": base["id"], "claims": d["counts"]}


def base_questions(base: dict | None) -> list[dict]:
    """The base CV's lines the career database doesn't back, as questions for you."""
    claims = (db.jload((base or {}).get("claims"), {}) or {}).get("claims") or []
    return [{"id": f"base:{n}", "kind": "claim", "where": "cv", "text": c["text"], "status": c["status"],
             "fact_ids": c.get("fact_ids") or [],
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
    """Your answers about the base CV. True as written: suggested for your career database (see `place`), and counted
    as true by the fact checks meanwhile. Not true: the CV says your words instead (or the line is taken out when you
    write nothing). Then the base is checked again."""
    from . import cvdata

    asked = {q["id"]: q for q in base_questions(base)}
    data = base["data"]
    place_items: list[dict] = []
    changed = 0
    missed: list[str] = []
    for qid, answer in answers.items():
        q = asked.get(qid)
        verdict, text = (answer.get("verdict") or "").strip(), (answer.get("text") or "").strip()
        if not q or verdict not in ("yes", "no"):
            continue
        if verdict == "yes":
            place_items.append(_item({**q, "kind": "cv_line"}, text))
        elif replace_in_cv(data, q["text"], text):
            changed += 1
        else:
            missed.append(q["text"])
    if place_items:  # your word: true from now on, whatever the database says or waits to say
        claims = db.jload(base.get("claims"), {}) or {}
        claims["confirmed"] = list(dict.fromkeys([*(claims.get("confirmed") or []), *(it["text"] for it in place_items)]))
        db.update("cv_bases", base["id"], {"claims": db.jdump(claims)})
    if changed:
        cvdata.save_base(base["id"], data)
    if place_items or changed:
        _queue(place_items, "your base CV", "cv-base-check", ("check_cv_base", {"base_id": base["id"]}))
    return {"placing": len(place_items), "changed": changed, "missed": missed}


def open_drafts() -> list[int]:
    """Applications you haven't sent, whose CV was made from the base."""
    return [r["id"] for r in db.q("SELECT id FROM applications WHERE status IN ('draft', 'ready') AND cv_data IS NOT NULL ORDER BY id")]
