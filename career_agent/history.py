"""History: every change to your data is recorded first, so anything can be undone or restored.

Each database connection the app opens gets temporary triggers on the tables that hold your data. When a row is
added, changed or deleted, the trigger writes what it was and what it became into `changes` (whole rows for adds and
deletes, only the changed columns for edits), tagged with the operation running at the time: something you did in
the app, or a background task. Undoing an operation puts every row it touched back as it was.

The triggers live only on the app's own connections, so changes made outside the app (the sqlite3 command, scripts)
aren't recorded. The daily copy of the database covers those.
"""
from __future__ import annotations

import contextlib
import contextvars
import shutil
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import date

from . import config, db

# The tables that hold your data. Not here: passages and embeddings (rebuilt from documents and facts), the model-call
# log, the task queue and sync bookkeeping.
TRACKED = ("settings", "documents", "facts", "companies", "sources", "jobs", "applications", "app_versions", "events",
           "watched_folders", "todos", "cv_bases", "fact_suggestions")
UNTRACKED_SETTINGS = ("state",)  # bookkeeping the scheduler rewrites every minute

NOUNS = {"settings": ("setting", "settings"), "documents": ("document", "documents"), "facts": ("fact", "facts"),
         "companies": ("company", "companies"), "sources": ("job source", "job sources"), "jobs": ("job", "jobs"),
         "applications": ("application", "applications"), "app_versions": ("saved draft", "saved drafts"),
         "events": ("timeline event", "timeline events"), "watched_folders": ("watched folder", "watched folders"),
         "todos": ("to-do", "to-dos"), "cv_bases": ("base CV", "base CVs"),
         "fact_suggestions": ("suggested change", "suggested changes")}
NAME_FIELDS = ("title", "name", "filename", "label", "subject", "key", "summary", "type", "url")

KEEP_AUTOMATIC_DAYS = 90  # your own changes are kept for good
KEEP_DAILY_COPIES = 7


@dataclass
class Op:
    id: int
    source: str  # "you" or "automatic"
    context: str
    undoes: int | None = None
    touched: bool = field(default=False)


_current: contextvars.ContextVar[Op | None] = contextvars.ContextVar("history_op", default=None)
_ids = threading.Lock()
_last_id = 0


def _new_id() -> int:
    """Ids grow with time, so operations sort by when they started, and stay unique across restarts."""
    global _last_id
    with _ids:
        _last_id = max(_last_id + 1, time.time_ns() // 1000)
        return _last_id


def _op_for_trigger() -> int | None:
    o = _current.get()
    if o is None:
        return None
    o.touched = True
    return o.id


@contextlib.contextmanager
def op(context: str, source: str = "automatic"):
    """Everything changed inside belongs to one operation (one entry in History). Nested calls join the outer one."""
    outer = _current.get()
    if outer is not None:
        yield outer
        return
    o = Op(_new_id(), source, context[:300])
    token = _current.set(o)
    try:
        yield o
    finally:
        _current.reset(token)
        # An operation that changed nothing (or whose changes were rolled back) leaves no entry.
        if o.touched and db.q1("SELECT 1 FROM changes WHERE op_id = ? LIMIT 1", (o.id,)):
            db.ex("INSERT OR IGNORE INTO change_ops (id, at, source, context, undoes) VALUES (?, ?, ?, ?, ?)",
                  (o.id, db.now(), o.source, o.context, o.undoes))


def current() -> Op | None:
    return _current.get()


# ---------------------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------------------

def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _obj(cols: list[str], prefix: str) -> str:
    """json_object(...) of a whole row, in chunks so no SQL function gets too many arguments."""
    first, rest = cols[:50], cols[50:]
    expr = "json_object(" + ", ".join(f"'{c}', {prefix}.{_q(c)}" for c in first) + ")"
    for i in range(0, len(rest), 50):
        expr = "json_set(" + expr + ", " + ", ".join(f"'$.{_q(c)}', {prefix}.{_q(c)}" for c in rest[i:i + 50]) + ")"
    return expr


def _diff(cols: list[str], side: str) -> str:
    """JSON of only the columns an UPDATE changed, as they were (OLD) or became (NEW). Unchanged ones go to a dummy
    key that's removed at the end, so a column that was NULL is still recorded as null."""
    expr = "'{}'"
    for i in range(0, len(cols), 50):
        parts = ", ".join(f"CASE WHEN OLD.{_q(c)} IS NOT NEW.{_q(c)} THEN '$.{_q(c)}' ELSE '$.\"~\"' END, {side}.{_q(c)}"
                          for c in cols[i:i + 50])
        expr = f"json_insert({expr}, {parts})"
    return f"json_remove({expr}, '$.\"~\"')"


def _columns(conn: sqlite3.Connection, table: str) -> tuple[list[str], str | None]:
    info = conn.execute(f"PRAGMA main.table_info({_q(table)})").fetchall()
    cols = [r[1] for r in info]
    pks = [r[1] for r in info if r[5]]
    return cols, (pks[0] if len(pks) == 1 else None)


def attach(conn: sqlite3.Connection) -> None:
    """Install the recording triggers on this connection, built from the tables' current columns."""
    if not conn.execute("SELECT 1 FROM main.sqlite_master WHERE type = 'table' AND name = 'changes'").fetchone():
        return  # the history tables come with a migration; init() attaches again once they exist
    conn.create_function("history_op", 0, _op_for_trigger)
    now = "strftime('%Y-%m-%dT%H:%M:%S', 'now')"
    for table in TRACKED:
        cols, pk = _columns(conn, table)
        if not cols or not pk:
            continue
        skip_new = f" WHEN NEW.key NOT IN ({', '.join(repr(k) for k in UNTRACKED_SETTINGS)})" if table == "settings" else ""
        skip_old = skip_new.replace("NEW.", "OLD.")
        changed = " OR ".join(f"OLD.{_q(c)} IS NOT NEW.{_q(c)}" for c in cols)
        upd_when = f" WHEN ({changed})" + (skip_new.replace(" WHEN", " AND") if skip_new else "")
        insert = f"INSERT INTO main.changes (op_id, at, tbl, row_key, action, before, after) VALUES (history_op(), {now}, '{table}'"
        for name, sql in (
            ("ins", f"AFTER INSERT ON main.{_q(table)}{skip_new} BEGIN {insert}, NEW.{_q(pk)}, 'insert', NULL, {_obj(cols, 'NEW')}); END"),
            ("upd", f"AFTER UPDATE ON main.{_q(table)}{upd_when} BEGIN {insert}, OLD.{_q(pk)}, 'update', "
                    f"{_diff(cols, 'OLD')}, {_diff(cols, 'NEW')}); END"),
            ("del", f"AFTER DELETE ON main.{_q(table)}{skip_old} BEGIN {insert}, OLD.{_q(pk)}, 'delete', {_obj(cols, 'OLD')}, NULL); END"),
        ):
            conn.execute(f"DROP TRIGGER IF EXISTS temp.history_{table}_{name}")
            conn.execute(f"CREATE TEMP TRIGGER history_{table}_{name} {sql}")


# ---------------------------------------------------------------------------------------
# Reading the history
# ---------------------------------------------------------------------------------------

def _row(table: str, key) -> dict | None:
    _, pk = _columns(db.connect(), table)
    return db.q1(f"SELECT * FROM {_q(table)} WHERE {_q(pk)} = ?", (key,))


def _name(table: str, data: dict | None) -> str:
    data = data or {}
    for f in NAME_FIELDS:
        if data.get(f):
            return str(data[f])[:120]
    return ""


def describe_change(ch: dict) -> dict:
    """A change, ready to show: what, which row, and field by field what it was and became."""
    before, after = db.jload(ch["before"], {}), db.jload(ch["after"], {})
    name = _name(ch["tbl"], after) or _name(ch["tbl"], before)
    if not name and ch["action"] == "update":
        name = _name(ch["tbl"], _row(ch["tbl"], ch["row_key"]))
    fields = []
    if ch["action"] == "update":
        fields = [(k, before.get(k), after.get(k)) for k in after]
    else:
        data = before if ch["action"] == "delete" else after
        fields = [(k, v, None) if ch["action"] == "delete" else (k, None, v) for k, v in data.items() if v not in (None, "")]
    return {**ch, "noun": NOUNS.get(ch["tbl"], (ch["tbl"], ch["tbl"]))[0], "name": name, "fields": fields}


_VERB = {"insert": "Added", "update": "Edited", "delete": "Deleted"}


def summary(changes: list[dict]) -> str:
    if not changes:
        return "No changes"
    if len(changes) == 1:
        c = describe_change(changes[0])
        what = f"{_VERB[c['action']]} {c['noun']}" + (f" “{c['name']}”" if c["name"] else f" #{c['row_key']}")
        if c["action"] == "update":
            what += f" ({', '.join(k for k, _, _ in c['fields'])})"
        return what
    counts: dict[tuple[str, str], set] = {}
    for c in changes:
        counts.setdefault((c["action"], c["tbl"]), set()).add(c["row_key"])
    parts = []
    for action in ("delete", "update", "insert"):
        for (a, t), keys in counts.items():
            if a == action:
                one, many = NOUNS.get(t, (t, t))
                parts.append(f"{_VERB[a].lower()} {len(keys)} {one if len(keys) == 1 else many}")
    text = ", ".join(parts)
    return text[0].upper() + text[1:]


def ops(show: str = "all", before: int | None = None, limit: int = 50) -> list[dict]:
    where, params = [], []
    if show in ("you", "automatic"):
        where.append("o.source = ?")
        params.append(show)
    if show == "deleted":
        where.append("EXISTS (SELECT 1 FROM changes c WHERE c.op_id = o.id AND c.action = 'delete')")
    if before:
        where.append("o.id < ?")
        params.append(before)
    rows = db.q("SELECT o.* FROM change_ops o" + (" WHERE " + " AND ".join(where) if where else "")
                + " ORDER BY o.id DESC LIMIT ?", (*params, limit))
    for r in rows:
        r["changes"] = db.q("SELECT * FROM changes WHERE op_id = ? ORDER BY id", (r["id"],))
        r["summary"] = summary(r["changes"])
        if r["undoes"]:  # named after what it undid
            undone = db.q("SELECT * FROM changes WHERE op_id = ? ORDER BY id", (r["undoes"],))
            r["summary"] = f"Undid: {summary(undone)}" if undone else "Undid an earlier change"
    return rows


def get_op(op_id: int) -> dict | None:
    return db.q1("SELECT * FROM change_ops WHERE id = ?", (op_id,))


def recently_deleted(table: str = "facts", limit: int = 30) -> list[dict]:
    """Rows deleted from `table` that haven't come back, newest first (the latest deletion of each)."""
    _, pk = _columns(db.connect(), table)
    rows = db.q(f"SELECT c.* FROM changes c WHERE c.tbl = ? AND c.action = 'delete' "
                f"AND c.id = (SELECT max(c2.id) FROM changes c2 WHERE c2.tbl = c.tbl AND c2.row_key = c.row_key AND c2.action = 'delete') "
                f"AND NOT EXISTS (SELECT 1 FROM {_q(table)} t WHERE t.{_q(pk)} = c.row_key) ORDER BY c.id DESC LIMIT ?", (table, limit))
    return [{**r, "row": db.jload(r["before"], {})} for r in rows]


# ---------------------------------------------------------------------------------------
# Undo and restore
# ---------------------------------------------------------------------------------------

class Conflict(Exception):
    def __init__(self, problems: list[str], forceable: bool = True):
        super().__init__("; ".join(problems))
        self.problems = problems
        self.forceable = forceable


def _label(table: str, key, data: dict | None) -> str:
    name = _name(table, data)
    return f"the {NOUNS.get(table, (table,))[0]} " + (f"“{name}”" if name else f"#{key}")


def _differs(row: dict, expected: dict) -> list[str]:
    return [k for k, v in expected.items() if k in row and row[k] != v]


def _insert(conn, table: str, data: dict) -> None:
    cols, _ = _columns(conn, table)
    data = {k: v for k, v in data.items() if k in cols}
    conn.execute(f"INSERT INTO {_q(table)} ({', '.join(_q(k) for k in data)}) VALUES ({', '.join('?' for _ in data)})",
                 tuple(data.values()))


def _apply_reverse(conn, ch: dict, force: bool) -> tuple[str | None, bool]:
    """Undo one change on the open transaction. Returns (problem, applied)."""
    table, key = ch["tbl"], ch["row_key"]
    cols, pk = _columns(conn, table)
    if not pk:
        return None, False
    before, after = db.jload(ch["before"], {}), db.jload(ch["after"], {})
    row = conn.execute(f"SELECT * FROM {_q(table)} WHERE {_q(pk)} = ?", (key,)).fetchone()
    row = dict(row) if row else None
    what = _label(table, key, after or before or row)
    if ch["action"] == "insert":  # it was added: take it out again
        if row is None:
            return None, False  # already gone
        changed = _differs(row, after)
        if changed and not force:
            return f"{what} was changed again afterwards ({', '.join(changed)})", False
        conn.execute(f"DELETE FROM {_q(table)} WHERE {_q(pk)} = ?", (key,))
        return None, True
    if ch["action"] == "update":  # it was edited: put the old values back
        if row is None:
            return f"{what} has been deleted since", False
        changed = _differs(row, after)
        if changed and not force:
            return f"{what} was changed again afterwards ({', '.join(changed)})", False
        values = {k: v for k, v in before.items() if k in cols}
        if values:
            conn.execute(f"UPDATE {_q(table)} SET {', '.join(f'{_q(k)} = ?' for k in values)} WHERE {_q(pk)} = ?",
                         (*values.values(), key))
        return None, True
    # it was deleted: bring it back with its old id
    if row is not None:
        if not force:
            return f"{what} exists again (it was restored or re-added since)", False
        conn.execute(f"DELETE FROM {_q(table)} WHERE {_q(pk)} = ?", (key,))
    _insert(conn, table, before)
    return None, True


def _after_restore(touched: dict[str, set]) -> None:
    """Rebuild what's derived from restored rows."""
    from . import retrieval, voice

    for doc_id in touched.get("documents", ()):
        if db.q1("SELECT 1 FROM documents WHERE id = ?", (doc_id,)):
            retrieval.index_document(int(doc_id))
    if touched.get("documents"):
        voice.compute_baseline()


def _run(changes: list[dict], force: bool, context: str, undoes: int | None = None) -> Op:
    with op(context, source="you") as o:
        if undoes and o.undoes is None:
            o.undoes = undoes
        problems, touched = [], {}
        try:
            with db.transaction() as conn:
                for ch in changes:
                    problem, applied = _apply_reverse(conn, ch, force)
                    if problem:
                        problems.append(problem)
                    elif applied:
                        touched.setdefault(ch["tbl"], set()).add(ch["row_key"])
                if problems:
                    raise Conflict(problems)
        except sqlite3.IntegrityError as e:
            raise Conflict([f"It can't be put back as it was: {e}. Something it depends on has changed since."], forceable=False) from e
        _after_restore(touched)
    return o


def undo(op_id: int, force: bool = False) -> int:
    """Put back everything one operation changed. Raises Conflict when something was changed again since (unless
    `force`). Returns the id of the new operation, which can itself be undone."""
    changes = db.q("SELECT * FROM changes WHERE op_id = ? ORDER BY id DESC", (op_id,))
    if not changes:
        raise Conflict(["There's nothing to undo in that entry."], forceable=False)
    o = _run(changes, force, f"Undo #{op_id}", undoes=op_id)
    db.ex("UPDATE change_ops SET undone_by = ? WHERE id = ?", (o.id, op_id))
    return o.id


def restore(change_id: int, force: bool = False) -> int:
    """Bring back one deleted row (from Recently deleted), whatever else its operation did."""
    ch = db.q1("SELECT * FROM changes WHERE id = ? AND action = 'delete'", (change_id,))
    if not ch:
        raise Conflict(["That deletion isn't in the history any more."], forceable=False)
    return _run([ch], force, f"Restore of change #{change_id}").id


# ---------------------------------------------------------------------------------------
# Keeping it tidy, and the daily copy
# ---------------------------------------------------------------------------------------

def prune(days: int = KEEP_AUTOMATIC_DAYS) -> int:
    """Forget automatic edits and additions older than `days` (mostly jobs being scored again). Your own changes and
    every deletion are kept, so anything deleted can always be restored."""
    cutoff = db.days_ago(days)
    with db.transaction() as conn:
        n = conn.execute("DELETE FROM changes WHERE at < ? AND action <> 'delete' AND (op_id IS NULL OR op_id NOT IN "
                         "(SELECT id FROM change_ops WHERE source <> 'automatic'))", (cutoff,)).rowcount
        conn.execute("DELETE FROM change_ops WHERE source = 'automatic' AND at < ? "
                     "AND NOT EXISTS (SELECT 1 FROM changes c WHERE c.op_id = change_ops.id)", (cutoff,))
    return n


def daily_copy(keep: int = KEEP_DAILY_COPIES) -> str | None:
    """A full copy of the database, at most one a day, keeping the newest `keep`. It covers changes the history
    can't see, such as edits made outside the app."""
    folder = config.DATA_DIR / "snapshots"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"career-{date.today().isoformat()}.db"
    made = None
    if not path.exists():
        tmp = path.with_suffix(".db.part")
        dst = sqlite3.connect(tmp)
        try:
            db.connect().backup(dst)
        finally:
            dst.close()
        shutil.move(tmp, path)
        made = str(path)
    for old in sorted(folder.glob("career-*.db"))[:-keep]:
        old.unlink(missing_ok=True)
    return made
