"""History: every change can be undone, deletions can be restored, and the facts tab groups facts by place."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from career_agent import config, db, history, profile, settings, tasks
from career_agent.web.app import app

from .test_web import _seed


def _fact(title: str, **kw) -> int:
    with history.op("seed", source="you"):
        return db.insert("facts", {"category": "project", "title": title, "created_at": db.now(), "verified": 1, **kw})


def _row(table: str, row_id: int) -> dict | None:
    return db.q1(f"SELECT * FROM {table} WHERE id = ?", (row_id,))


def _undo_id(location: str) -> int:
    return int(location.split("undo=")[1].split("&")[0].split("#")[0])


def test_a_deleted_fact_comes_back_exactly_as_it_was(fake):
    fid = _fact("Video game modding", organization="Personal", detail="Ghidra reverse engineering", tags="confidential")
    before = _row("facts", fid)
    c = TestClient(app)
    r = c.post(f"/facts/{fid}/delete", follow_redirects=False)
    assert r.status_code == 303 and "undo=" in r.headers["location"] and _row("facts", fid) is None
    page = c.get(r.headers["location"]).text
    assert ">Undo</button>" in page, "the message after a change offers to undo it"
    assert "Recently deleted" in page and "Video game modding" in page
    r = c.post(f"/history/{_undo_id(r.headers['location'])}/undo", data={"next": "/profile?tab=facts"}, follow_redirects=False)
    assert r.status_code == 303 and _row("facts", fid) == before, "same id, same fields"
    assert ">Redo</button>" in c.get(r.headers["location"]).text, "and the undo can be undone in turn"


def test_restore_one_fact_from_recently_deleted(fake):
    keep, gone = _fact("Kept"), _fact("Merged away")
    with history.op("Tidying up your career database"):  # a tidy-up that removed two facts in one go
        db.ex("DELETE FROM facts WHERE id IN (?, ?)", (keep, gone))
    deleted = history.recently_deleted("facts")
    assert {d["row"]["title"] for d in deleted} == {"Kept", "Merged away"}
    change = next(d for d in deleted if d["row"]["title"] == "Kept")
    r = TestClient(app).post(f"/history/change/{change['id']}/restore", data={"next": "/profile?tab=facts"}, follow_redirects=False)
    assert r.status_code == 303 and _row("facts", keep) and not _row("facts", gone)
    assert [d["row"]["title"] for d in history.recently_deleted("facts")] == ["Merged away"]


def test_undoing_an_edit_that_was_changed_again_asks_first(fake):
    fid = _fact("Go")
    with history.op("first edit", source="you") as first:
        db.update("facts", fid, {"title": "Go (Golang)", "detail": "Microservices"})
    with history.op("second edit", source="you"):
        db.update("facts", fid, {"title": "Golang"})
    with pytest.raises(history.Conflict) as e:
        history.undo(first.id)
    assert "changed again" in e.value.problems[0] and _row("facts", fid)["title"] == "Golang", "nothing changed"
    r = TestClient(app).post(f"/history/{first.id}/undo", data={}, follow_redirects=False)
    assert "force_ok=1" in r.headers["location"] and "Not+undone" in r.headers["location"]
    history.undo(first.id, force=True)
    assert (_row("facts", fid)["title"], _row("facts", fid)["detail"]) == ("Go", None)


def test_an_application_comes_back_with_its_drafts_and_timeline(fake):
    doc_id, job_id, app_id = _seed()
    with history.op("save", source="you"):
        db.insert("app_versions", {"application_id": app_id, "label": "v1", "letter": "Kære Mette", "created_at": db.now()})
    versions = db.q("SELECT * FROM app_versions WHERE application_id = ? ORDER BY id", (app_id,))
    events = db.q("SELECT * FROM events WHERE application_id = ? ORDER BY id", (app_id,))
    application = _row("applications", app_id)
    assert versions and events
    c = TestClient(app)
    r = c.post(f"/applications/{app_id}/delete", follow_redirects=False)
    assert not _row("applications", app_id) and not db.q("SELECT 1 FROM app_versions WHERE application_id = ?", (app_id,))
    c.post(f"/history/{_undo_id(r.headers['location'])}/undo", data={})
    assert _row("applications", app_id) == application
    assert db.q("SELECT * FROM app_versions WHERE application_id = ? ORDER BY id", (app_id,)) == versions
    assert db.q("SELECT * FROM events WHERE application_id = ? ORDER BY id", (app_id,)) == events


def test_a_restored_document_gets_its_facts_and_passages_back(fake):
    doc_id, _, _ = _seed()
    linked = [r["id"] for r in db.q("SELECT id FROM facts WHERE document_id = ?", (doc_id,))]
    passages = db.scalar("SELECT COUNT(*) FROM chunks WHERE document_id = ?", (doc_id,))
    assert linked and passages
    c = TestClient(app)
    r = c.post(f"/documents/{doc_id}/delete", data={}, follow_redirects=False)
    assert not _row("documents", doc_id) and not db.q("SELECT 1 FROM facts WHERE document_id = ?", (doc_id,))
    c.post(f"/history/{_undo_id(r.headers['location'])}/undo", data={})
    assert _row("documents", doc_id)
    assert sorted(r["id"] for r in db.q("SELECT id FROM facts WHERE document_id = ?", (doc_id,))) == sorted(linked)
    assert db.scalar("SELECT COUNT(*) FROM chunks WHERE document_id = ?", (doc_id,)) == passages


def test_background_work_is_automatic_and_old_automatic_edits_are_forgotten(fake, monkeypatch):
    fid = _fact("A")
    monkeypatch.setattr(tasks, "_handlers", lambda: {"score_job": lambda p: db.update("facts", fid, {"title": "B"})})
    task_id = db.insert("tasks", {"kind": "score_job", "payload": "{}", "status": "queued", "created_at": db.now()})
    tasks.run_task(db.q1("SELECT * FROM tasks WHERE id = ?", (task_id,)))
    entry = history.ops("automatic")[0]
    assert (entry["context"], entry["source"]) == ("Scoring fit", "automatic") and entry["summary"].startswith("Edited fact")
    with history.op("Tidying up your career database"):
        db.ex("DELETE FROM facts WHERE id = ?", (fid,))
    db.ex("UPDATE changes SET at = '2020-01-01T00:00:00'")
    db.ex("UPDATE change_ops SET at = '2020-01-01T00:00:00'")
    history.prune()
    left = db.q("SELECT c.action, o.source FROM changes c JOIN change_ops o ON o.id = c.op_id ORDER BY c.id")
    assert left == [{"action": "insert", "source": "you"}, {"action": "delete", "source": "automatic"}], \
        "your own changes and every deletion stay; old automatic edits go"
    assert history.recently_deleted("facts")[0]["row"]["title"] == "B"


def test_what_is_not_recorded(fake):
    n = db.scalar("SELECT COUNT(*) FROM changes")  # the test setup's own settings are recorded
    with history.op("x", source="you"):
        settings.update("state", {"last_digest": "2026-11-17"})  # rewritten by the scheduler every minute
        db.insert("tasks", {"kind": "score_job", "payload": "{}", "status": "queued", "created_at": db.now()})
    assert db.scalar("SELECT COUNT(*) FROM changes") == n, "bookkeeping and the task queue aren't history"
    other = sqlite3.connect(config.DB_PATH)  # outside the app: covered by the daily copy instead
    other.execute("INSERT INTO facts (category, title, created_at) VALUES ('skill', 'Outside', 'x')")
    other.commit()
    other.close()
    assert db.scalar("SELECT COUNT(*) FROM changes") == n


def test_a_new_column_is_recorded_too(fake):
    conn = db.connect()
    conn.execute("ALTER TABLE todos ADD COLUMN colour TEXT")
    history.attach(conn)  # what init() does after every migration
    with history.op("x", source="you") as o:
        db.insert("todos", {"title": "T", "created_at": db.now(), "colour": "red"})
    assert json.loads(db.q1("SELECT after FROM changes WHERE op_id = ?", (o.id,))["after"])["colour"] == "red"


def test_daily_copy_once_a_day_keeping_seven(fake):
    folder = config.DATA_DIR / "snapshots"
    folder.mkdir()
    for d in range(1, 10):
        (folder / f"career-2020-01-{d:02d}.db").write_bytes(b"old")
    _fact("In the copy")
    made = history.daily_copy()
    assert made and history.daily_copy() is None, "at most one a day"
    names = sorted(p.name for p in folder.glob("career-*.db"))
    assert len(names) == 7 and names[-1] == Path(made).name
    assert sqlite3.connect(made).execute("SELECT title FROM facts WHERE title = 'In the copy'").fetchone()


def test_history_page(fake):
    fid = _fact("Shown in history")
    c = TestClient(app)
    c.post(f"/facts/{fid}/delete")
    for show in ("all", "you", "automatic", "deleted"):
        r = c.get(f"/history?show={show}")
        assert r.status_code == 200
    page = c.get("/history?show=deleted").text
    assert "Deleted fact “Shown in history”" in page and "Restore just this" in page


def test_facts_grouped_by_place_and_time():
    def f(i, cat, org, start=None, end=None):
        return {"id": i, "category": cat, "title": f"t{i}", "organization": org, "start_date": start, "end_date": end}

    facts = [f(1, "experience", "Nordvik P/S", "2026-05", "present"), f(2, "project", "Nordvik"),
             f(3, "education", "Nordhavn University College", "2022", "2026-01"),
             f(4, "project", "Nordhavn University College, Campus Horsens", "2022-10", "2022-12"),
             f(5, "project", "Nordhavn University College, Campus Horsens", "2024-02", "2024-05"),
             f(6, "project", "Brinkholm Horsens, Teknisk Gymnasium HTX", "2020-10", "2020-12"),
             f(7, "project", "Brinkholm HTX", "2021-08", "2021-10"), f(8, "project", None),
             f(9, "skill", "Havbro"), f(10, "experience", "Havbro ApS", "2024-08", "2024-12")]
    index = profile.places(facts)
    sections = profile.by_place([x for x in facts if x["category"] == "project"], index)
    assert [(place, years) for place, years, _ in sections] == [
        ("Nordvik P/S", "2026 – present"), ("Nordhavn University College", "2022 – 2026"),
        ("Brinkholm HTX", "2020 – 2021"), ("No place given", "")]  # spellings tied: the shortest names it
    assert [x["id"] for x in sections[1][2]] == [5, 4], "newest first within a place"
    assert profile.by_place([facts[8]], index) == [("Havbro ApS", "2024", [facts[8]])]
