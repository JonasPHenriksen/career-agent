"""Your to-do list: the things only you can do, in order, at the top of Today.

An item can wait for a date (`show_from`): until then it's listed under "Coming up"; on the day it moves into the
list and you get one notification. An item can also carry an automatic progress line (`check_kind`), e.g. how many
facts you've ticked so far. Agents and scripts add items with `add()`.
"""
from __future__ import annotations

import re
from datetime import date

from . import db, notify

CHECKS = ("facts", "collect")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _date(value: str | None) -> str | None:
    v = (value or "").strip()
    if not _DATE.match(v):
        return None
    try:
        return date.fromisoformat(v).isoformat()
    except ValueError:
        return None


def add(title: str, detail: str = "", href: str = "", show_from: str | None = None,
        check_kind: str | None = None) -> int:
    title = (title or "").strip()
    if not title:
        raise ValueError("A to-do needs a title.")
    position = (db.scalar("SELECT MAX(position) FROM todos") or 0) + 1
    return db.insert("todos", {
        "title": title[:300], "detail": (detail or "").strip()[:2000] or None, "href": (href or "").strip() or None,
        "check_kind": check_kind if check_kind in CHECKS else None, "show_from": _date(show_from),
        "position": position, "created_at": db.now(),
    })


def progress(item: dict) -> str:
    if item.get("check_kind") == "facts":
        total = db.scalar("SELECT COUNT(*) FROM facts WHERE hidden = 0") or 0
        ticked = db.scalar("SELECT COUNT(*) FROM facts WHERE hidden = 0 AND verified = 1") or 0
        return f"{ticked} of {total} facts ticked"
    if item.get("check_kind") == "collect":
        from . import collect

        rows = collect.searches()
        fresh = len(rows) - len(collect.due_searches())
        return f"{fresh} of {len(rows)} saved searches collected in the last {collect.REMIND_DAYS} days"
    return ""


def lists(today: str | None = None) -> dict:
    """{'now': open items to do now, in order; 'later': dated items still waiting; 'done': the last few done}."""
    today = today or date.today().isoformat()
    rows = db.q("SELECT * FROM todos WHERE done_at IS NULL ORDER BY position, id")
    now = [r for r in rows if not r["show_from"] or r["show_from"] <= today]
    later = sorted((r for r in rows if r["show_from"] and r["show_from"] > today), key=lambda r: r["show_from"])
    for r in now:
        r["progress"] = progress(r)
        if r.get("check_kind") == "collect":
            from . import collect

            r["searches"] = collect.search_links()
    done = db.q("SELECT * FROM todos WHERE done_at IS NOT NULL ORDER BY done_at DESC, id DESC LIMIT 5")
    return {"now": now, "later": later, "done": done}


def count_now(today: str | None = None) -> int:
    today = today or date.today().isoformat()
    return db.scalar("SELECT COUNT(*) FROM todos WHERE done_at IS NULL AND (show_from IS NULL OR show_from <= ?)",
                     (today,)) or 0


def set_done(todo_id: int, done: bool = True) -> None:
    db.update("todos", todo_id, {"done_at": db.now() if done else None})


def delete(todo_id: int) -> None:
    db.ex("DELETE FROM todos WHERE id = ?", (todo_id,))


def move(todo_id: int, step: int) -> None:
    """Swap with the open neighbour above (step -1) or below (step +1)."""
    rows = db.q("SELECT id, position FROM todos WHERE done_at IS NULL ORDER BY position, id")
    ids = [r["id"] for r in rows]
    if todo_id not in ids:
        return
    i = ids.index(todo_id)
    j = i + (1 if step > 0 else -1)
    if not 0 <= j < len(ids):
        return
    ids[i], ids[j] = ids[j], ids[i]
    with db.transaction() as conn:
        conn.executemany("UPDATE todos SET position = ? WHERE id = ?", [(n, tid) for n, tid in enumerate(ids, 1)])


def notify_due(today: str | None = None) -> int:
    """One notification for each dated item whose day has come. Runs with the app's maintenance."""
    today = today or date.today().isoformat()
    rows = db.q("SELECT * FROM todos WHERE done_at IS NULL AND notified_at IS NULL AND show_from IS NOT NULL "
                "AND show_from <= ?", (today,))
    for r in rows:
        notify.send("Reminder", r["title"])
        db.update("todos", r["id"], {"notified_at": db.now()})
    return len(rows)
