"""Changes a model suggests for your career database, waiting for you.

Nothing a model comes up with goes into the career database by itself: the facts it reads from a document, what your
answers in a review add to a fact, two facts a tidy-up takes for the same thing. Each becomes a suggestion under
Profile → Career database → Pending changes, which you approve (as it is, or edited first) or refuse. Approving puts
it in, marked as checked by you; a refused suggestion isn't made again. What you change by hand goes in directly.

Changes the app made by itself before suggestions existed are listed the same way ("already in"), so you can keep
or undo each one.
"""
from __future__ import annotations

from . import db, settings

# What a fact says. The other columns (verified, hidden, source, absorbed, dates of the row) are bookkeeping.
FIELDS = ("category", "title", "organization", "location", "start_date", "end_date", "detail", "evidence", "tags",
          "area", "depth")
LABELS = {"title": "Title", "organization": "Organisation", "location": "Place", "start_date": "From",
          "end_date": "To", "detail": "Details", "evidence": "Evidence", "category": "Type", "tags": "Tags",
          "area": "Area", "depth": "How well you know it", "document_id": "Source document"}
OPEN = ("pending", "applied")  # waiting for you: suggested, or already in and waiting to be kept or undone


class Problem(ValueError):
    """A suggestion that can't be applied as it is; the message says why, for the page."""


def _blank(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def _same(a, b) -> bool:
    return (None if _blank(a) else a) == (None if _blank(b) else b)


def _norm(s: str | None) -> str:
    from .profile import _norm as norm

    return norm(s)


def added_text(before: str | None, after: str | None) -> str:
    """The words `after` adds to the end of `before`, or "" when it changes more than that."""
    b, a = (before or "").strip(), (after or "").strip()
    return a[len(b):].strip() if b and a.startswith(b) and len(a) > len(b) else ""


def _with_added(current: str | None, adds: str) -> str:
    return " ".join(x for x in ((current or "").strip(), adds.strip()) if x)


def _row(s: dict) -> dict:
    return {**s, "data": db.jload(s.get("data"), {}) or {}, "before": db.jload(s.get("before"), None)}


def get(sid: int) -> dict | None:
    s = db.q1("SELECT * FROM fact_suggestions WHERE id = ?", (sid,))
    return _row(s) if s else None


# ---------------------------------------------------------------------------------------------------------------
# Making suggestions
# ---------------------------------------------------------------------------------------------------------------

def refused_titles() -> set[str]:
    """New facts you refused, by title: reading a document again doesn't suggest them again."""
    return {_norm(r["title"]) for r in db.q("SELECT title FROM fact_suggestions WHERE action = 'new' AND status = 'refused'")}


def waiting_new() -> list[dict]:
    """New facts suggested and still waiting."""
    return [_row(s) for s in db.q("SELECT * FROM fact_suggestions WHERE action = 'new' AND status = 'pending' ORDER BY id")]


def suggest_new(fact: dict, why: str, origin: str, ref: str | None = None, document_id: int | None = None) -> int | None:
    """A new fact for you to approve. None when you refused the same one before or it is already waiting."""
    data = {k: fact[k] for k in FIELDS if not _blank(fact.get(k))}
    title = (data.get("title") or "").strip()
    if not title:
        return None
    key = _norm(title)
    if key in refused_titles():
        return None
    for s in waiting_new():
        if _norm(s["title"]) == key and s["data"].get("category") == data.get("category"):
            return None
    return db.insert("fact_suggestions", {
        "action": "new", "status": "pending", "title": title[:300], "category": data.get("category"),
        "data": db.jdump(data), "why": (why or "")[:600], "origin": origin, "ref": ref,
        "document_id": document_id, "created_at": db.now()})


def update_waiting(sid: int, patch: dict) -> None:
    """Fill in a new fact that is still waiting (a document said more about it): nothing in the database changes."""
    s = get(sid)
    if not s or s["status"] != "pending":
        return
    data = {**s["data"], **{k: v for k, v in patch.items() if k in FIELDS or k == "document_id"}}
    db.update("fact_suggestions", sid, {"data": db.jdump(data), "title": (data.get("title") or s["title"])[:300]})


def suggest_edit(fact: dict, changes: dict, why: str, origin: str, ref: str | None = None,
                 document_id: int | None = None) -> int | None:
    """A change to a fact in your database, for you to approve. Only fields that really change count; None when
    nothing does, or the same change is already waiting or was refused."""
    changes = {k: v for k, v in changes.items()
               if (k in FIELDS or k == "document_id") and not _same(fact.get(k), v)}
    if not changes:
        return None
    for s in db.q("SELECT * FROM fact_suggestions WHERE action = 'edit' AND fact_id = ? AND status IN ('pending', 'refused')",
                  (fact["id"],)):
        if (db.jload(s["data"], {}) or {}) == changes:
            return None
    adds = added_text(fact.get("detail"), changes["detail"]) if set(changes) == {"detail"} else ""
    return db.insert("fact_suggestions", {
        "action": "edit", "status": "pending", "title": fact["title"][:300], "category": fact.get("category"),
        "fact_id": fact["id"], "data": db.jdump(changes), "before": db.jdump({k: fact.get(k) for k in changes}),
        "adds": adds or None, "why": (why or "")[:600], "origin": origin, "ref": ref, "document_id": document_id,
        "created_at": db.now()})


def suggest_merge(keep: dict, other: dict, changes: dict, why: str, origin: str = "tidy",
                  ref: str | None = "facts") -> int | None:
    """Two facts that look like the same thing: `other` merged into `keep` (with `changes` to it) and removed."""
    pair = {keep["id"], other["id"]}
    for s in db.q("SELECT fact_id, other_id FROM fact_suggestions WHERE action = 'merge' AND status IN ('pending', 'refused')"):
        if {s["fact_id"], s["other_id"]} == pair:
            return None
    changes = {k: v for k, v in changes.items() if k in FIELDS and not _same(keep.get(k), v)}
    return db.insert("fact_suggestions", {
        "action": "merge", "status": "pending", "title": keep["title"][:300], "category": keep.get("category"),
        "fact_id": keep["id"], "other_id": other["id"], "data": db.jdump(changes),
        "before": db.jdump({k: keep.get(k) for k in changes}), "why": (why or "")[:600], "origin": origin,
        "ref": ref, "created_at": db.now()})


def drop_waiting(document_id: int) -> int:
    """A document read again: what its last reading suggested and you haven't decided yet makes way for the new
    reading. Returns how many were dropped."""
    n = db.scalar("SELECT COUNT(*) FROM fact_suggestions WHERE document_id = ? AND status = 'pending'", (document_id,)) or 0
    db.ex("DELETE FROM fact_suggestions WHERE document_id = ? AND status = 'pending'", (document_id,))
    return n


# ---------------------------------------------------------------------------------------------------------------
# Deciding
# ---------------------------------------------------------------------------------------------------------------

def _fact(fid: int | None) -> dict | None:
    return db.q1("SELECT * FROM facts WHERE id = ?", (fid,)) if fid else None


def _edited(form: dict | None) -> dict:
    """The fields you edited before approving, as the database keeps them."""
    out = {}
    for k in FIELDS:
        if form and k in form:
            v = form[k]
            out[k] = v.strip() if isinstance(v, str) and v.strip() else None
    return out


def approve(sid: int, edited: dict | None = None) -> dict:
    """Put the suggestion in your career database, marked as checked by you; `edited` is the fact as you changed it
    first. Returns {"fact_id", "said"}."""
    s = get(sid)
    if not s:
        raise Problem("That suggestion is gone.")
    if s["status"] == "applied":  # already in: approving it keeps it
        db.update("fact_suggestions", sid, {"status": "approved", "decided_at": db.now()})
        return {"fact_id": s["fact_id"], "said": f"Kept: “{s['title']}”."}
    if s["status"] != "pending":
        raise Problem("That suggestion was already decided.")
    mine = _edited(edited)
    now = db.now()
    if s["action"] == "new":
        row = {k: v for k, v in {**s["data"], **mine}.items() if k in FIELDS}
        if _blank(row.get("title")):
            raise Problem("A fact needs a title.")
        from .profile import CATEGORY_LABELS

        if row.get("category") not in CATEGORY_LABELS:
            row["category"] = "project"
        doc = s["document_id"] if s["document_id"] and db.q1("SELECT 1 FROM documents WHERE id = ?", (s["document_id"],)) else None
        row.update({"document_id": doc, "source": "extracted" if doc else "manual", "verified": 1, "created_at": now})
        fid = db.insert("facts", row)
        said = f"Added to your career database: “{row['title']}”."
    elif s["action"] == "edit":
        fact = _fact(s["fact_id"])
        if not fact:
            raise Problem("The fact it would change has been deleted since.")
        if mine:
            patch = {k: v for k, v in mine.items() if not _same(fact.get(k), v)}
        else:
            patch = dict(s["data"])
            if s.get("adds") and not _same(fact.get("detail"), (s["before"] or {}).get("detail")):
                patch["detail"] = _with_added(fact.get("detail"), s["adds"])  # the fact changed since: add to it as it is now
        if patch.get("document_id") and not db.q1("SELECT 1 FROM documents WHERE id = ?", (patch["document_id"],)):
            patch.pop("document_id")
        db.update("facts", fact["id"], {**patch, "verified": 1, "updated_at": now})
        fid, said = fact["id"], f"Changed in your career database: “{fact['title']}”."
    elif s["action"] == "merge":
        keep, other = _fact(s["fact_id"]), _fact(s["other_id"])
        if not keep:
            raise Problem("The fact it would keep has been deleted since.")
        patch = {k: v for k, v in mine.items() if not _same(keep.get(k), v)} if mine else dict(s["data"])
        db.update("facts", keep["id"], {**patch, "verified": 1, "updated_at": now})
        if other:
            from .profile import remember_absorbed

            remember_absorbed(keep["id"], [other["title"], *(db.jload(other.get("absorbed"), []) or [])])
            db.ex("DELETE FROM facts WHERE id = ?", (other["id"],))
        fid, said = keep["id"], f"Merged into “{keep['title']}”" + (f"; “{other['title']}” is removed." if other else ".")
    else:
        raise Problem("Unknown kind of suggestion.")
    db.update("fact_suggestions", sid, {"status": "approved", "decided_at": now,
                                        **({"data": db.jdump({**s["data"], **mine})} if mine else {})})
    return {"fact_id": fid, "said": said}


def refuse(sid: int) -> dict:
    """Leave the suggestion out; it isn't made again. For a change that's already in, put the fact back as it was."""
    s = get(sid)
    if not s:
        raise Problem("That suggestion is gone.")
    if s["status"] == "applied":
        fact = _fact(s["fact_id"])
        if fact:
            moved = [k for k, v in s["data"].items() if not _same(fact.get(k), v)]
            if moved:
                raise Problem(f"“{fact['title']}” has been changed since, so it can't simply be put back. Edit it by hand.")
            db.update("facts", fact["id"], {**(s["before"] or {}), "updated_at": db.now()})
        db.update("fact_suggestions", sid, {"status": "refused", "decided_at": db.now()})
        return {"fact_id": s["fact_id"], "said": f"Put back as it was: “{s['title']}”."}
    if s["status"] != "pending":
        raise Problem("That suggestion was already decided.")
    db.update("fact_suggestions", sid, {"status": "refused", "decided_at": db.now()})
    return {"fact_id": s["fact_id"], "said": f"Left out: “{s['title']}”. It won't be suggested again."}


def decide_all(document_id: int, approve_them: bool) -> tuple[int, list[str]]:
    """Approve or refuse everything a document's reading suggested. Returns (how many, problems)."""
    done, problems = 0, []
    for s in db.q("SELECT id FROM fact_suggestions WHERE document_id = ? AND status = 'pending' ORDER BY id", (document_id,)):
        try:
            (approve if approve_them else refuse)(s["id"])
            done += 1
        except Problem as e:
            problems.append(str(e))
    return done, problems


# ---------------------------------------------------------------------------------------------------------------
# Showing them
# ---------------------------------------------------------------------------------------------------------------

def count() -> int:
    listed_before()
    return db.scalar("SELECT COUNT(*) FROM fact_suggestions WHERE status IN ('pending', 'applied')") or 0


def _link(ref: str | None) -> str:
    if not ref:
        return ""
    kind, _, key = ref.partition(":")
    if kind == "app" and key.isdigit():
        return f"/applications/{key}?tab=review"
    if kind == "doc" and key.isdigit():
        return f"/documents/{key}"
    if ref == "cv-base-check":
        return "/profile?tab=cv#cv-check"
    return ""


def _shown(fact: dict) -> list[tuple[str, str]]:
    from .profile import CATEGORY_LABELS

    out = []
    for k in ("organization", "location", "start_date", "end_date", "detail", "evidence"):
        if not _blank(fact.get(k)):
            out.append((LABELS[k], str(fact[k])))
    if fact.get("category"):
        out.insert(0, (LABELS["category"], CATEGORY_LABELS.get(fact["category"], fact["category"])))
    return out


def _value(k: str, v) -> str:
    if _blank(v):
        return ""
    if k == "category":
        from .profile import CATEGORY_LABELS

        return CATEGORY_LABELS.get(v, v)
    if k == "document_id":
        d = db.q1("SELECT title, filename FROM documents WHERE id = ?", (v,))
        return (d["title"] or d["filename"]) if d else f"document #{v}"
    return str(v)


def waiting() -> list[dict]:
    """What waits for you, ready to show: already-made changes first, then the suggestions, oldest first."""
    listed_before()
    rows = db.q("SELECT * FROM fact_suggestions WHERE status IN ('pending', 'applied') "
                "ORDER BY status = 'applied' DESC, COALESCE(document_id, 0), id")
    docs = {d["id"]: d for d in db.q("SELECT id, title, filename FROM documents")}
    out = []
    for raw in rows:
        s = _row(raw)
        fact, other = _fact(s["fact_id"]), _fact(s["other_id"])
        before = s["before"] or {}
        s["link"] = _link(s["ref"])
        s["doc"] = docs.get(s["document_id"])
        s["gone"] = s["action"] in ("edit", "merge") and not fact
        s["stale"] = bool(fact) and any(not _same(fact.get(k), v) for k, v in before.items()) and s["status"] == "pending"
        if s["action"] == "new":
            s["result"] = s["data"]
            s["shown"] = _shown(s["data"])
        else:
            s["result"] = {**(fact or {}), **s["data"]}
            if s["stale"] and s.get("adds") and fact:
                s["result"]["detail"] = _with_added(fact.get("detail"), s["adds"])
            s["changes"] = [(LABELS.get(k, k), _value(k, before.get(k)), _value(k, v)) for k, v in s["data"].items()
                            if k != "detail" or not s.get("adds")]
        s["fact"], s["other"] = fact, other
        out.append(s)
    return out


# ---------------------------------------------------------------------------------------------------------------
# Changes made before suggestions existed
# ---------------------------------------------------------------------------------------------------------------

def listed_before() -> int:
    """Once: the app's own changes to facts from the last 60 days (your answers put in by themselves), listed as
    already in, so you can keep or undo each. Changes the fact has moved on from since aren't listed."""
    state = settings.get("state")
    if state.get("quiet_changes_listed"):
        return 0
    n = 0
    rows = db.q("SELECT c.*, o.context FROM changes c JOIN change_ops o ON o.id = c.op_id WHERE c.tbl = 'facts' "
                "AND c.action = 'update' AND o.source = 'automatic' AND c.at >= ? ORDER BY c.id", (db.days_ago(60),)) \
        if db.q1("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'changes'") else []
    for ch in rows:
        before, after = db.jload(ch["before"], {}) or {}, db.jload(ch["after"], {}) or {}
        content = {k: v for k, v in after.items() if k in FIELDS}
        fact = _fact(int(ch["row_key"])) if str(ch["row_key"]).isdigit() else None
        if not content or not fact or any(not _same(fact.get(k), v) for k, v in content.items()):
            continue
        adds = added_text(before.get("detail"), content["detail"]) if set(content) == {"detail"} else ""
        when = (ch["at"] or "")[:10]
        db.insert("fact_suggestions", {
            "action": "edit", "status": "applied", "title": fact["title"][:300], "category": fact.get("category"),
            "fact_id": fact["id"], "data": db.jdump(content), "before": db.jdump({k: before.get(k) for k in content}),
            "adds": adds or None, "origin": "before", "created_at": ch["at"] or db.now(),
            "why": f"Changed by the app itself on {when} ({(ch['context'] or 'a background task').lower()}), "
                   "before changes waited for you."})
        n += 1
    settings.update("state", {"quiet_changes_listed": True})
    return n
