"""The career database: analyse documents into facts, merge duplicates, build prompt digests."""
from __future__ import annotations

import re
import unicodedata

from . import db, jobkinds, llm, settings
from .llm import jsonutil as J

CATEGORIES = [
    ("experience", "Experience"),
    ("education", "Education"),
    ("project", "Projects"),
    ("utility", "Smaller programs and utilities"),
    ("skill", "Skills"),
    ("certification", "Certifications"),
    ("language", "Languages"),
    ("about", "About me"),
]
CATEGORY_LABELS = dict(CATEGORIES)

# Kinds from before the database was reorganised. A result ("achievement") is no longer a fact of its own: it is
# written into the job, education or project it came from. Facts that still arrive with an old kind (an older
# facts.jsonl, a model that ignores the list) are filed under the new one.
LEGACY_CATEGORIES = {"achievement": "project", "value": "about", "personal": "about", "volunteering": "about"}

# Skills are grouped by area, and say how well you know them, so "insight only" is never written up as experience.
AREAS = ["Languages", "Backend and data", "Web", "AI", "ERP (Dynamics 365)", "DevOps and infrastructure",
         "Embedded and XR", "Ways of working"]
DEPTHS = [("work", "used at work"), ("project", "used in a larger project"), ("coursework", "coursework"),
          ("insight", "insight only")]
DEPTH_LABELS = dict(DEPTHS)


def category_of(name: str | None) -> str | None:
    """The kind a fact is filed under, or None when `name` isn't a kind at all."""
    name = (name or "").strip()
    return name if name in CATEGORY_LABELS else LEGACY_CATEGORIES.get(name)

KINDS = [
    ("cv", "CV / résumé"),
    ("application", "Application / cover letter"),
    ("email", "Email"),
    ("reference", "Reference / review"),
    ("report", "Report / work document"),
    ("certificate", "Certificate / diploma"),
    ("job_ad", "Job ad"),
    ("course_material", "Course material, not about you (not used)"),
    ("other", "Other"),
    ("unknown", "Not analysed yet"),
]
KIND_LABELS = dict(KINDS)

OUTCOMES = [
    ("unknown", "Outcome unknown"),
    ("no_response", "No response"),
    ("rejected", "Rejected"),
    ("interview", "Got an interview"),
    ("offer", "Got an offer"),
]
OUTCOME_LABELS = dict(OUTCOMES)
OUTCOME_RANK = {"offer": 0, "interview": 1, "unknown": 2, "rejected": 3, "no_response": 4}

ANALYZE_SCHEMA = J.obj(
    kind=J.s("Document type", ["cv", "application", "email", "reference", "report", "certificate", "job_ad",
                               "course_material", "other"]),
    language=J.s("ISO 639-1 code of the document's main language, e.g. da or en"),
    title=J.s("Short descriptive title, max 8 words"),
    doc_date=J.s("When it was written or issued: YYYY-MM-DD, YYYY-MM or YYYY. Empty if unknown"),
    summary=J.s("One or two sentences about the document"),
    written_by_candidate=J.b("True if the candidate wrote this text alone"),
    target_company=J.s("For applications: the company applied to. Otherwise empty"),
    target_role=J.s("For applications: the role applied for. Otherwise empty"),
    facts=J.arr(J.obj(
        category=J.s("Fact type", [c for c, _ in CATEGORIES]),
        title=J.s("Job title / degree / one-line achievement / skill name / etc."),
        organization=J.s("Employer, school, client or issuer. Empty if none"),
        location=J.s("City or country if stated, else empty"),
        start_date=J.s("YYYY-MM or YYYY. Empty if not stated"),
        end_date=J.s("YYYY-MM or YYYY, 'present' if ongoing. Empty if not stated"),
        detail=J.s("What the document says about it, 1-3 sentences, facts only"),
        evidence=J.s("Short verbatim quote from the document that supports this fact"),
        tags=J.arr(J.s(), "Keywords: skills, tools, domains, sectors"),
    )),
)

ANALYZE_SYSTEM = """You extract a factual career database from documents that belong to {who}. The database is later used to write job applications, so accuracy matters more than completeness: an invented or embellished fact could cost the candidate an interview.

First classify the document:
- kind: cv | application (cover letter or job application, including unsolicited ones) | email | reference (recommendation, reference letter, review written by others) | report (any work product: report, presentation, article, thesis, case) | certificate (diploma, course certificate, transcript) | job_ad (a job advertisement) | course_material (lecture slides, reading material, textbooks, assignment descriptions, templates, articles and other texts written by others that aren't about the candidate) | other
- written_by_candidate: true if the candidate wrote the text alone (their CV, applications, emails, solo reports). False for group work written with others, references, certificates, job ads, course material and other people's texts.

Then extract facts about the candidate, one item per fact:
- experience: one per position. title = job title, organization = employer, plus dates and location. detail = scope and responsibilities, then what the job as a whole led to (feedback, a reference).
- education: one per degree or programme. title = degree, organization = institution. detail = subjects and awards. Leave grades and pass marks out.
- project: one per named or clearly described project, product or initiative the candidate worked on. organization = where it happened. detail = what it was, then the candidate's own part, then the result.
- utility: a small program, script or exercise (hours or a few days of work) that isn't worth a project entry.
- skill: one per distinct skill, tool, method or domain the candidate demonstrably has. title = the skill name only. detail = where or how it was used.
- certification: certificates, licences, courses with a certificate.
- language: spoken languages. title = language name in English, detail = level as stated.
- about: what motivates the candidate, how they like to work, and other personal facts they share (driving licence, interests, side activities), only when they say it themselves.

A result or accomplishment ("Cut onboarding time from 3 weeks to 1 week") is never a fact of its own: put it in the detail of the project, job or education it came from, with its numbers exactly as written.

Rules:
- Only extract what the document states. Never infer, generalise or improve. If a number, title or date is not in the text, leave it out.
- evidence is a short verbatim quote copied from the document.
- Write title and detail in the document's own language.
- For job ads and texts that are not about the candidate, return an empty facts list.
- Skip contact details, ID numbers and other identifiers.
- At most 60 facts. Prefer the most specific and useful ones."""


def _limits() -> dict:
    """Input budgets, sized for the model behind each tier."""
    cfg = settings.get("llm")

    steps = cfg.get("steps") or {}

    def local(tier: str) -> bool:
        own = steps.get("letter" if tier == "writing" else "score") or {}  # the step that reads the most
        provider = own["provider"] if own.get("provider") and own.get("model") else (cfg.get(tier) or {}).get("provider")
        return provider == "ollama"

    num_ctx = int(cfg.get("ollama_num_ctx") or 32768)
    small = num_ctx <= 32768
    return {
        "doc_chars": 36_000 if local("analysis") and small else 150_000,
        "facts_chars_writing": 22_000 if local("writing") and small else 90_000,
        "facts_chars_analysis": 22_000 if local("analysis") and small else 90_000,
        "exemplar_words": 1_500 if local("writing") and small else 4_000,
        "style_words": 4_000 if local("writing") and small else 12_000,
        "job_chars": 12_000 if (local("writing") or local("analysis")) and small else 40_000,
        "small_writer": local("writing") and small,  # sizes the agent-folder summaries in the prompt
    }


def limits() -> dict:
    return _limits()


def analyze_document(doc_id: int, replace: bool = False) -> dict:
    """Read a document into facts. With `replace` (a redo with another model), the document's unticked facts are
    removed first so the new reading replaces them; facts you've ticked or hidden stay."""
    doc = db.q1("SELECT * FROM documents WHERE id = ?", (doc_id,))
    if not doc:
        return {"skipped": "document deleted"}
    if replace:
        db.ex("DELETE FROM facts WHERE document_id = ? AND verified = 0 AND hidden = 0", (doc_id,))
    db.update("documents", doc_id, {"status": "analyzing", "error": None})
    name = settings.get("profile").get("name") or ""
    who = f"the job seeker {name}" if name else "a job seeker (the candidate)"
    parts = analysis_parts(doc["text"], _limits()["doc_chars"])
    results = []
    for n, part in enumerate(parts):
        note = (f"\n(This is part {n + 1} of {len(parts)} of a long document. Extract the facts in this part; "
                "for the classification fields, judge the document as a whole as far as you can see it.)"
                if len(parts) > 1 else "")
        user = (f"File name: {doc['filename']}{note}\n"
                + (f"Imported as: {doc['kind']}\n" if doc["kind"] not in ("unknown", "") else "")
                + f"\n<document>\n{part}\n</document>")
        try:
            r = llm.run("analysis", "analyze_document", ANALYZE_SYSTEM.format(who=who), user,
                        ANALYZE_SCHEMA, max_tokens=16000, effort="medium")
        except llm.LLMError as e:
            db.update("documents", doc_id, {"status": "error", "error": str(e)})
            raise
        results.append(r.data)
    d = dict(results[0])
    d["facts"] = [f for res in results for f in res["facts"]]
    kind = doc["kind"] if doc["kind"] not in ("unknown", "") else d["kind"]
    authored = 1 if d["written_by_candidate"] or kind in ("cv", "application") else 0
    if kind in ("reference", "certificate", "job_ad", "course_material"):
        authored = 0
    db.update("documents", doc_id, {
        "kind": kind,
        "language": J.lang_code(d["language"]),
        "title": d["title"] or doc["title"],
        "doc_date": d["doc_date"] or doc["doc_date"],
        "summary": d["summary"],
        "target_company": d["target_company"] or None,
        "target_role": d["target_role"] or None,
        "authored": authored,
        "status": "analyzed",
        "analyzed_at": db.now(),
    })
    # Course material says what was taught, not what the candidate did: it adds no facts.
    facts = [] if kind in ("job_ad", "course_material") else [_clean_dates(f, doc["text"]) for f in d["facts"]]
    added, merged = save_facts(doc_id, facts, authoritative=(kind == "cv"))
    return {"kind": kind, "facts_added": added, "facts_merged": merged}


MAX_PARTS = 12


def analysis_parts(text: str, max_chars: int, max_parts: int = MAX_PARTS) -> list[str]:
    """Split a long document at paragraph boundaries into pieces the model can read whole."""
    if len(text) <= max_chars:
        return [text]
    paras = re.split(r"(\n\s*\n)", text)
    parts, buf = [], ""
    for piece in paras:
        while len(piece) > max_chars:  # one giant paragraph (e.g. PDF text): cut at a line break
            cut = piece.rfind("\n", 0, max_chars)
            cut = cut if cut > max_chars // 2 else max_chars
            if buf:
                parts.append(buf)
                buf = ""
            parts.append(piece[:cut])
            piece = piece[cut:]
        if len(buf) + len(piece) > max_chars and buf.strip():
            parts.append(buf)
            buf = piece
        else:
            buf += piece
    if buf.strip():
        parts.append(buf)
    parts = [p.strip() for p in parts if p.strip()]
    return parts[:max_parts]


_PRESENT = {"present", "now", "current", "today", "nu", "i dag", "nuværende", "dato", "d.d.", "ongoing", "pågående"}


def _clean_dates(f: dict, text: str) -> dict:
    """Keep only dates that actually appear in the document (local models sometimes compute or guess them)."""
    for key in ("start_date", "end_date"):
        v = (f.get(key) or "").strip()
        if not v:
            continue
        if key == "end_date" and v.lower() in _PRESENT:
            f[key] = "present"
            continue
        m = re.match(r"^(\d{4})(?:-(\d{1,2}))?", v)
        if not m or m.group(1) not in text:
            f[key] = ""
            continue
        month = int(m.group(2)) if m.group(2) else 0
        f[key] = m.group(1) + (f"-{month:02d}" if 1 <= month <= 12 else "")
    if f.get("end_date") == "present" and not f.get("start_date"):
        f["end_date"] = ""
    return f


# ---------------------------------------------------------------------------------------
# Facts: dedupe & merge
# ---------------------------------------------------------------------------------------

def _norm(s: str | None) -> str:
    s = unicodedata.normalize("NFKD", (s or "").lower())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"\b(a/s|aps|ab|as|asa|gmbh|ltd|inc|llc|i/s|p/s)\b", " ", s)
    return re.sub(r"[^\w]+", " ", s).strip()


_STOP = {"and", "the", "for", "with", "og", "med", "til", "for", "som", "der", "det", "den", "af", "at", "in", "of", "on", "to", "fra", "har", "var", "were", "was"}


def _tokens(s: str) -> set[str]:
    return {w for w in _norm(s).split() if len(w) > 2 and w not in _STOP}


def _similar(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _year(d: str | None) -> str:
    m = re.match(r"(\d{4})", d or "")
    return m.group(1) if m else ""


def _span(f: dict) -> tuple[int, int] | None:
    s = _year(f.get("start_date"))
    if not s:
        return None
    e = 9999 if (f.get("end_date") or "") == "present" else int(_year(f.get("end_date")) or s)
    return int(s), e


def _same_org(a: str | None, b: str | None) -> bool:
    x, y = _norm(a), _norm(b)
    return bool(x == y or (x and y and (x in y or y in x)))


_NUM = re.compile(r"(?<![\d.,])(\d+(?:[.,]\d+)?)(?![\d.,])")


def _numbers(s: str) -> set[str]:
    return {n.replace(",", ".") for n in _NUM.findall(s or "") if not re.fullmatch(r"(19|20)\d\d", n)}


def _is_duplicate(cat: str, e: dict, f: dict, same_org_count: int = 0) -> bool:
    if cat in ("skill", "language"):
        return _norm(e["title"]) == _norm(f["title"])
    if cat in ("experience", "education", "certification"):
        if not _same_org(e.get("organization"), f.get("organization")):
            return False
        if _similar(e["title"], f["title"]) >= 0.5:
            return True
        se, sf = _span(e), _span(f)
        if se and sf:  # the same employer in overlapping years is the same role, described differently
            return (se[0] < sf[1] and sf[0] < se[1]) or se[0] == sf[0]
        return cat == "experience" and same_org_count == 1
    return _similar(f"{e['title']} {e.get('detail') or ''}", f"{f['title']} {f.get('detail') or ''}") >= 0.55 \
        or _norm(e["title"]) == _norm(f["title"])


def _same_result(e: dict, f: dict) -> bool:
    """A result (`f`) that the entry `e` already states: the same place and a number in common."""
    if not _same_org(e.get("organization"), f.get("organization")):
        return False
    return bool(_numbers(f"{e['title']} {e.get('detail') or ''}") & _numbers(f"{f['title']} {f.get('detail') or ''}"))


def remember_absorbed(keep_id: int, titles: list[str]) -> None:
    """Note on a fact which other facts were merged into it, so reading a document or importing the agent folder
    again doesn't bring them back as facts of their own."""
    row = db.q1("SELECT absorbed FROM facts WHERE id = ?", (keep_id,))
    if not row:
        return
    known = db.jload(row["absorbed"], []) or []
    new = [t for t in dict.fromkeys(_norm(t) for t in titles) if t and t not in known]
    if new:
        db.update("facts", keep_id, {"absorbed": db.jdump(known + new)})


def _recency(doc_date: str | None, mtime: float | None) -> str:
    """Comparable 'when was this written': the document's own date, else the file's modification date."""
    if doc_date and re.match(r"\d{4}", doc_date):
        return doc_date
    if mtime:
        from datetime import datetime

        return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")
    return ""


def save_facts(doc_id: int | None, facts: list[dict], authoritative: bool = False) -> tuple[int, int]:
    """Insert facts, merging duplicates. `authoritative` (a CV) wins over vaguer versions from letters,
    and between two CVs the more recent one wins, whatever order they're read in."""
    added = merged = 0
    this = db.q1("SELECT doc_date, source_mtime FROM documents WHERE id = ?", (doc_id,)) if doc_id else None
    this_when = _recency(this["doc_date"], this["source_mtime"]) if this else ""
    existing: dict[str, list[dict]] = {}
    gone = set(settings.get("dismissed_facts") or [])  # titles you removed on purpose, and titles merged into another fact
    for row in db.q("SELECT f.*, d.kind AS doc_kind, d.doc_date AS src_date, d.source_mtime AS src_mtime "
                    "FROM facts f LEFT JOIN documents d ON d.id = f.document_id"):
        existing.setdefault(row["category"], []).append(row)
        gone.update(db.jload(row.get("absorbed"), []) or [])
    for f in facts:
        title = (f.get("title") or "").strip()
        said = (f.get("category") or "about").strip()
        cat = category_of(said)
        if not title or not cat:
            continue
        if _norm(title) in gone:  # already part of another fact, or removed by you: it doesn't come back
            merged += 1
            continue
        if said == "achievement" and any(_same_result(e, f) for c in ("project", "experience", "education")
                                         for e in existing.get(c, [])):
            merged += 1  # the job, education or project it belongs to already states this result
            continue
        tags = ", ".join(t.strip() for t in (f.get("tags") or []) if t and t.strip())[:300]
        same_org_count = sum(1 for e in existing.get(cat, []) if _same_org(e.get("organization"), f.get("organization")))
        dup = next((e for e in existing.get(cat, []) if _is_duplicate(cat, e, f, same_org_count)), None)
        if dup:
            patch = {}
            dup_when = _recency(dup.get("src_date"), dup.get("src_mtime"))
            newer_cv = bool(this_when and dup_when and this_when > dup_when)
            override = authoritative and not dup["verified"] and (dup.get("doc_kind") != "cv" or newer_cv)
            for key in ("title", "organization", "location", "start_date", "end_date", "evidence"):
                if f.get(key) and (not dup.get(key) or (override and key != "evidence")):
                    patch[key] = f[key]
            # A longer description wins, except over one from a CV: only a (newer) CV replaces that.
            longer = len(f.get("detail") or "") > len(dup.get("detail") or "") and dup.get("doc_kind") != "cv"
            if f.get("detail") and not dup["verified"] and (override or longer):
                patch["detail"] = f["detail"]
            if override and doc_id:
                patch["document_id"] = doc_id
                dup.update({"doc_kind": "cv", "src_date": this["doc_date"] if this else None,
                            "src_mtime": this["source_mtime"] if this else None})
            if tags:
                merged_tags = {t.strip() for t in (dup.get("tags") or "").split(",") if t.strip()} | set(tags.split(", "))
                patch["tags"] = ", ".join(sorted(merged_tags))[:400]
            if patch:
                patch["updated_at"] = db.now()
                db.update("facts", dup["id"], patch)
                dup.update(patch)
            merged += 1
            continue
        row = {
            "category": cat, "title": title[:300], "organization": (f.get("organization") or "")[:200] or None,
            "location": (f.get("location") or "")[:120] or None,
            "start_date": (f.get("start_date") or "")[:10] or None, "end_date": (f.get("end_date") or "")[:10] or None,
            "detail": (f.get("detail") or "")[:1500] or None, "evidence": (f.get("evidence") or "")[:600] or None,
            "tags": tags or None, "document_id": doc_id, "source": "extracted" if doc_id else "manual",
            "created_at": db.now(),
        }
        row["id"] = db.insert("facts", row)
        row.update({"verified": 0, "hidden": 0, "doc_kind": "cv" if authoritative else None,
                    "src_date": this["doc_date"] if this else None, "src_mtime": this["source_mtime"] if this else None})
        existing.setdefault(cat, []).append(row)
        added += 1
    return added, merged


# ---------------------------------------------------------------------------------------
# Digests for prompts
# ---------------------------------------------------------------------------------------

def _period(f: dict) -> str:
    s, e = f.get("start_date") or "", f.get("end_date") or ""
    if s and e:
        return f"{s} – {e}"
    return s or e


# Words that don't tell two places apart: company forms, and "the"/"of"/"university" before the actual name.
_PLACE_NOISE = {"aps", "a/s", "p/s", "i/s", "k/s", "ivs", "as", "ps", "the", "of", "university", "universitet"}


def place_key(org: str | None) -> str:
    """The first word that names an organisation, so spellings of one place group together: "Nordvik" and
    "Nordvik P/S", or "Brinkholm HTX" and "Brinkholm Horsens, Teknisk Gymnasium HTX". "" when there's none."""
    words = [w for w in re.findall(r"[\w/&]+", (org or "").lower())
             if w not in _PLACE_NOISE and any(ch.isalnum() for ch in w)]
    return words[0] if words else ""


def _years(starts: list[str], ends: list[str]) -> str:
    s = min(starts) if starts else ""
    e = "present" if "present" in ends else max(ends) if ends else ""
    if s and e:
        return s if s == e else f"{s} – {e}"
    return f"from {s}" if s else f"until {e}" if e else ""


def places(facts: list[dict]) -> dict[str, dict]:
    """The places your facts happened at, with a name and the years they span. A place is named after your job or
    education there when there is one, else after its most used spelling; its years come from that job or
    education, else from all its facts. Newest first; at the same time, a place you worked or studied at first."""
    out: dict[str, dict] = {}
    for f in facts:
        key = place_key(f.get("organization"))
        p = out.setdefault(key, {"key": key, "names": {}, "anchor": [], "facts": []})
        p["facts"].append(f)
        org = (f.get("organization") or "").strip()
        if org:
            p["names"][org] = p["names"].get(org, 0) + 1
            if f["category"] in ("experience", "education"):
                p["anchor"].append(f)
    for p in out.values():
        names = {}
        for f in p["anchor"]:
            names[f["organization"].strip()] = names.get(f["organization"].strip(), 0) + 1
        names = names or p["names"]  # the most used spelling, the shortest when they're tied
        named = min(names.items(), key=lambda kv: (-kv[1], len(kv[0])))[0] if names else ""
        p["label"] = named or "No place given"
        dated = p["anchor"] or p["facts"]
        ends = ["present" if (f.get("end_date") or "").lower() == "present" else _year(f.get("end_date")) for f in dated]
        p["period"] = _years([y for f in dated if (y := _year(f.get("start_date")))], [e for e in ends if e]) if p["key"] else ""
        p["sort"] = (bool(p["key"]), 9999 if "present" in ends else int(max([e for e in ends if e] or ["0"])),
                     bool(p["anchor"]), max([_year(f.get("start_date")) or "0" for f in dated]))
    return out


def by_place(items: list[dict], index: dict[str, dict]) -> list[tuple[str, str, list[dict]]]:
    """`items` (one category) in sections per place, newest place first and newest fact first within it."""
    groups: dict[str, list[dict]] = {}
    for f in items:
        groups.setdefault(place_key(f.get("organization")), []).append(f)
    order = sorted(groups, key=lambda k: index[k]["sort"] if k in index else (False, 0, False, "0"), reverse=True)
    return [(index[k]["label"] if k in index else "No place given", index[k]["period"] if k in index else "",
             sorted(groups[k], key=lambda f: (f.get("start_date") or "", -f["id"]), reverse=True)) for k in order]


def by_area(items: list[dict]) -> list[tuple[str, str, list[dict]]]:
    """Skills in sections per area, in the fixed order; skills with no area come last."""
    groups: dict[str, list[dict]] = {}
    for f in items:
        groups.setdefault(f.get("area") if f.get("area") in AREAS else "", []).append(f)
    rank = {d: n for n, (d, _) in enumerate(DEPTHS)}
    return [(a or "Other", "", sorted(groups[a], key=lambda f: (rank.get(f.get("depth"), 9), f["title"].lower())))
            for a in [*AREAS, ""] if a in groups]


RECENT_DAYS = 30


def recently_added(limit: int = 10, days: int = RECENT_DAYS) -> tuple[list[dict], int]:
    """The facts added in the last `days` days, newest first, each with where it came from; and how many that is in all."""
    since = db.days_ago(days)
    rows = db.q("SELECT f.*, d.title AS doc_title, d.filename AS doc_filename FROM facts f "
                "LEFT JOIN documents d ON d.id = f.document_id WHERE f.created_at >= ? "
                "ORDER BY f.created_at DESC, f.id DESC LIMIT ?", (since, limit))
    for f in rows:
        tags = {t.strip() for t in (f.get("tags") or "").split(",")}
        f["kind_label"] = CATEGORY_LABELS.get(f["category"], f["category"])
        f["origin"] = (f"from {f['doc_title'] or f['doc_filename']}" if f.get("document_id")
                       else "your answer in a fact check" if "your answer" in tags
                       else "from a document since deleted" if f.get("source") == "extracted"
                       else "added by you")
    return rows, db.scalar("SELECT COUNT(*) FROM facts WHERE created_at >= ?", (since,)) or 0


def visible_facts() -> list[dict]:
    order = {c: n for n, (c, _) in enumerate(CATEGORIES)}
    rows = db.q("SELECT * FROM facts WHERE hidden = 0")
    rows.sort(key=lambda f: (order.get(f["category"], 99), -(int(_year(f.get("start_date")) or 0)), f["id"]))
    return rows


# When space is tight, the career skeleton is always kept whole; then projects (each with its result);
# smaller programs, skills and what you say about yourself give way first.
CORE_CATEGORIES = ("experience", "education", "certification", "language")
MAIN_CATEGORIES = ("project",)
MINOR_CATEGORIES = ("about", "utility", "skill")  # about me feeds the "why this job" part
_DETAIL_PLANS = [  # detail characters per group (or per kind, where one is named), from generous to tight
    {"core": 1000, "main": 1000, "about": 800, "utility": 400, "minor": 200},  # a whole entry: what, your part, result
    {"core": 320, "main": 320, "about": 200, "minor": 0},
    {"core": 160, "main": 140, "minor": 0},
    {"core": 120, "main": 90, "minor": 0},
]


def _fact_line(f: dict, detail_cap: int, with_evidence: bool) -> str:
    bits = [f"[#{f['id']}] {f['title']}"]
    if f.get("organization"):
        bits.append(f"— {f['organization']}")
    meta = ", ".join(x for x in (_period(f), f.get("location") or "") if x)
    if meta:
        bits.append(f"({meta})")
    line = " ".join(bits)
    if f.get("depth") in DEPTH_LABELS:
        line += f" [{DEPTH_LABELS[f['depth']]}]"
    if detail_cap and f.get("detail"):
        line += f": {f['detail'][:detail_cap]}"
    if with_evidence and f.get("evidence"):
        line += f' | evidence: "{f["evidence"][:200]}"'
    if f["verified"]:
        line += " ✓"
    return line


def _group(category: str) -> str:
    return "core" if category in CORE_CATEGORIES else "main" if category in MAIN_CATEGORIES else "minor"


def _render_digest(rows: list[dict], plan: dict, max_chars: int, with_evidence: bool,
                   rank: dict[int, int] | None = None) -> tuple[str, dict]:
    lines = {f["id"]: _fact_line(f, plan.get(f["category"], plan[_group(f["category"])]), with_evidence) for f in rows}
    headers = sum(len(label) + 5 for _, label in CATEGORIES)
    note_reserve = 320
    chosen = {f["id"] for f in rows if f["category"] in CORE_CATEGORIES}
    used = headers + note_reserve + sum(len(lines[i]) + 1 for i in chosen)
    # Projects get most of what's left, but skills and what you say about yourself keep a share:
    # they're short, and fit scoring and CV skills sections depend on them.
    remaining = max(0, max_chars - used)
    shares = {"main": int(remaining * 0.65), "minor": remaining - int(remaining * 0.65)}
    if rank:  # most relevant first within each group; what you say about yourself stays ahead of skills
        main = sorted((f for f in rows if f["category"] in MAIN_CATEGORIES), key=lambda f: rank.get(f["id"], 10**6))
        values = sorted((f for f in rows if f["category"] == "about"), key=lambda f: rank.get(f["id"], 10**6))
        rest = sorted((f for f in rows if f["category"] in MINOR_CATEGORIES and f["category"] != "about"),
                      key=lambda f: rank.get(f["id"], 10**6))
        ordered = main + values + rest
    else:
        ordered = [f for cat in MAIN_CATEGORIES + MINOR_CATEGORIES for f in rows if f["category"] == cat]
    for f in ordered:  # first pass: each group within its share
        g, cost = _group(f["category"]), len(lines[f["id"]]) + 1
        if cost <= shares[g]:
            chosen.add(f["id"])
            shares[g] -= cost
            used += cost
    for f in ordered:  # second pass: whatever space is left, in priority order
        cost = len(lines[f["id"]]) + 1
        if f["id"] not in chosen and used + cost <= max_chars:
            chosen.add(f["id"])
            used += cost
    dropped: dict[str, int] = {}
    for f in ordered:
        if f["id"] not in chosen:
            dropped[f["category"]] = dropped.get(f["category"], 0) + 1
    out, cur = [], None
    shown = [f for f in rows if f["id"] in chosen]
    if rank:
        cat_order = {c: n for n, (c, _) in enumerate(CATEGORIES)}
        shown.sort(key=lambda f: (cat_order.get(f["category"], 99), rank.get(f["id"], -1) if f["category"] not in CORE_CATEGORIES else 0))
    for f in shown:  # grouped by category
        if f["category"] != cur:
            cur = f["category"]
            out.append(f"\n## {CATEGORY_LABELS.get(cur, cur)}")
        out.append(lines[f["id"]])
    if dropped:
        left_out = ", ".join(f"{n} {CATEGORY_LABELS.get(c, c).lower()}" for c, n in dropped.items())
        why = "less relevant to this job, " if rank else ""
        out.append(f"\n(Not shown ({why}for space): {left_out}. Absence from this list doesn't mean the candidate lacks it.)")
    return "\n".join(out).strip(), dropped


def facts_digest(max_chars: int, with_evidence: bool = False, query: str | None = None) -> str:
    """Compact text of the career database with [#id] markers the model can cite.

    Roles, education, certifications and languages are always included in full. When everything
    doesn't fit, details are shortened first; then, if a `query` (e.g. the job ad) is given, the
    facts most relevant to it are kept (hybrid keyword + meaning search), otherwise the most important.
    """
    rows = visible_facts()
    if not rows:
        return "(The career database is empty.)"
    text, dropped = _render_digest(rows, _DETAIL_PLANS[0], max_chars, with_evidence)
    if not dropped:
        return text
    rank = None
    if query:
        from . import retrieval

        order = retrieval.rank_facts(query, [f for f in rows if f["category"] not in CORE_CATEGORIES])
        rank = {fid: n for n, fid in enumerate(order)}
    for plan in _DETAIL_PLANS:
        text, dropped = _render_digest(rows, plan, max_chars, with_evidence, rank)
        if not dropped:
            break
    return text


LEVELS = [("student", "Student"), ("junior", "Junior (up to about 2 years' experience)"), ("mid", "Mid-level"),
          ("senior", "Senior"), ("lead", "Lead / manager"), ("executive", "Executive")]
LEVEL_LABELS = dict(LEVELS)


def distance_limit() -> float | None:
    """The longest distance from home to on-site or hybrid work, in km, or None for no limit."""
    try:
        km = float(settings.get("profile").get("max_km") or 0)
    except (TypeError, ValueError):
        return None
    return km if km > 0 else None


def candidate_block() -> str:
    p = settings.get("profile")
    rows = [
        ("Name", p.get("name")), ("City", p.get("city")), ("Headline", p.get("headline")),
        ("Level", LEVEL_LABELS.get(p.get("level") or "")),
        ("Target roles", p.get("target_roles")), ("Target industries", p.get("target_industries")),
        ("Preferred locations", p.get("locations")),
        ("Longest distance to on-site or hybrid work",
         f"{distance_limit():g} km from {p.get('city')} in a straight line (measured for each job, never estimate it)"
         if distance_limit() and p.get("city") else None),
        ("Work modes", ", ".join(p.get("work_modes") or [])), ("Salary expectation", p.get("min_salary")),
        ("Availability / notice", p.get("availability")), ("Deal-breakers", p.get("deal_breakers")),
        ("Always mention when relevant", p.get("always_mention")), ("Never mention", p.get("never_mention")),
        ("In their own words", p.get("about")),
    ]
    wanted = jobkinds.wanted_kinds()
    off = [label for key, label in jobkinds.KINDS if not wanted[key]]
    if off:
        rows.append(("Not interested in", ", ".join(off)))
    return "\n".join(f"- {k}: {v}" for k, v in rows if v) or "- (no details given yet)"


def brief(max_skills: int = 30) -> str:
    """A few lines about the candidate, for quick screening prompts."""
    p = settings.get("profile")
    exp = db.q("SELECT title, organization, start_date, end_date FROM facts WHERE category = 'experience' "
               "AND hidden = 0 ORDER BY COALESCE(start_date, '') DESC LIMIT 6")
    skills = db.q("SELECT title FROM facts WHERE category = 'skill' AND hidden = 0 ORDER BY verified DESC, id LIMIT ?",
                  (max_skills,))
    edu = db.q("SELECT title, organization FROM facts WHERE category = 'education' AND hidden = 0 LIMIT 3")
    lines = []
    if p.get("headline"):
        lines.append(f"Headline: {p['headline']}")
    if exp:
        lines.append("Recent roles: " + "; ".join(
            f"{e['title']} at {e['organization'] or '?'} ({_period(e)})" for e in exp))
    if edu:
        lines.append("Education: " + "; ".join(f"{e['title']} ({e['organization'] or ''})" for e in edu))
    if skills:
        lines.append("Skills: " + ", ".join(s["title"] for s in skills))
    return "\n".join(lines) or "(no career data yet)"


CONSOLIDATE_SCHEMA = J.obj(groups=J.arr(J.obj(
    keep=J.i("id of the entry to keep"),
    duplicates=J.arr(J.i(), "ids of entries that describe the same thing as keep"),
)))
CONSOLIDATE_SYSTEM = """You clean up a career database that was extracted automatically from several documents, some in Danish and some in English. Find entries that describe the same thing: the same job, degree, project, skill or statement about the candidate, even when worded differently or written in another language.

For each group of duplicates, choose the entry to keep: prefer verified entries, then entries from the CV, then the most precise one (exact dates, exact numbers, clearest wording). Only group entries of the same type that clearly describe the same thing. Two different roles at the same employer are not duplicates. A broader skill can absorb a narrower duplicate (e.g. keep "Scrum og SAFe", merge "Scrum").
Return only groups that contain duplicates; return an empty list if there are none."""


def consolidate() -> dict:
    """Merge duplicate facts (e.g. the same achievement written in Danish and in English)."""
    rows = db.q("SELECT f.*, d.kind AS doc_kind FROM facts f LEFT JOIN documents d ON d.id = f.document_id "
                "WHERE f.hidden = 0 ORDER BY f.category, f.id")
    if len(rows) < 2:
        return {"merged": 0}
    lines = [f"[#{f['id']}] {f['category']} | {f['title']} | {f['organization'] or '-'} | {_period(f) or '-'} | "
             f"{(f['detail'] or '')[:140]} | from {f['doc_kind'] or 'manual entry'}{' | VERIFIED' if f['verified'] else ''}"
             for f in rows]
    r = llm.run("analysis", "consolidate_facts", CONSOLIDATE_SYSTEM, "\n".join(lines), CONSOLIDATE_SCHEMA,
                max_tokens=8000, effort="medium")
    by_id = {f["id"]: f for f in rows}
    merged = 0
    for g in r.data["groups"]:
        keep = by_id.get(g["keep"])
        if not keep:
            continue
        for dup_id in g["duplicates"]:
            d = by_id.get(dup_id)
            if not d or d["id"] == keep["id"] or d["category"] != keep["category"] or d["verified"]:
                continue
            patch = {k: d[k] for k in ("organization", "location", "start_date", "end_date", "evidence", "detail")
                     if d.get(k) and not keep.get(k)}
            tags = {t.strip() for t in f"{keep.get('tags') or ''},{d.get('tags') or ''}".split(",") if t.strip()}
            if tags:
                patch["tags"] = ", ".join(sorted(tags))[:400]
            if patch:
                patch["updated_at"] = db.now()
                db.update("facts", keep["id"], patch)
                keep.update(patch)
            remember_absorbed(keep["id"], [d["title"], *(db.jload(d.get("absorbed"), []) or [])])
            db.ex("DELETE FROM facts WHERE id = ?", (d["id"],))
            by_id.pop(d["id"], None)
            merged += 1
    return {"merged": merged}


def has_facts() -> bool:
    return bool(db.scalar("SELECT 1 FROM facts WHERE hidden = 0 LIMIT 1"))


def fact_ids_valid(ids: list[int]) -> list[int]:
    if not ids:
        return []
    marks = ",".join("?" for _ in ids)
    return [r["id"] for r in db.q(f"SELECT id FROM facts WHERE id IN ({marks})", ids)]
