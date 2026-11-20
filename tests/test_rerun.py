"""Running everything again with other models, and stopping it midway."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from career_agent import db, jobs, redo, tasks
from career_agent.web.app import app

from .conftest import JOB_TEXT


def _job(n: int, **cols) -> int:
    return db.insert("jobs", {"url": f"https://example.com/{n}", "title": f"Job {n}", "status": "new", "created_at": db.now(), **cols})


def test_a_full_redo_queues_every_step_in_the_order_it_depends_on(fake):
    sid = db.insert("sources", {"name": "s", "kind": "rss", "url": "https://example.com/rss", "created_at": db.now()})
    kept = _job(1, source_id=sid, parse_status="parsed", fit_score=70)
    dropped = _job(2, source_id=sid, status="filtered", parse_status="skipped")
    by_hand = _job(3, parse_status="parsed", fit_score=60)
    pasted = db.insert("jobs", {"description": JOB_TEXT, "title": "Pasted", "status": "new", "parse_status": "parsed", "created_at": db.now()})
    mine = _job(5, source_id=sid, status="dismissed", parse_status="parsed")

    found = redo.candidates()
    assert found["screen"] == [f"job:{kept}", f"job:{dropped}"], "listings from a source that you haven't acted on"
    assert found["ads"] == [f"job:{kept}", f"job:{by_hand}", f"job:{pasted}"] and f"job:{mine}" not in found["jobs"]

    counts = redo.enqueue(["jobs", "ads", "screen"], model="claude_code|claude-sonnet-5-5|high")
    assert counts == {"screen": 2, "ads": 3, "jobs": 0}, "an ad that is read again is scored as part of that"
    queued = [(t["kind"], json.loads(t["payload"])) for t in db.q("SELECT kind, payload FROM tasks ORDER BY id")]
    assert queued[0] == ("rescreen_jobs", {"model": "claude_code|claude-sonnet-5-5|high"}), "screening comes first, as one task"
    assert queued[1:] == [("parse_job", {"job_id": kept, "model": "claude_code|claude-sonnet-5-5|high"}),
                          ("parse_job", {"job_id": by_hand, "model": "claude_code|claude-sonnet-5-5|high"}),
                          ("parse_job", {"job_id": pasted, "use_text": True, "model": "claude_code|claude-sonnet-5-5|high"})]
    assert tasks.TASK_LABELS["rescreen_jobs"] and "rescreen_jobs" in tasks.TASK_STEPS


def test_the_queue_can_be_dropped_midway_and_stays_dropped(fake):
    job_id = jobs.add_from_text(JOB_TEXT)  # queues reading it
    running = db.insert("tasks", {"kind": "score_job", "payload": "{}", "status": "running", "created_at": db.now()})
    assert tasks.cancel_queued() == 1
    assert db.q1("SELECT status FROM tasks WHERE kind = 'parse_job'")["status"] == "cancelled"
    assert db.q1("SELECT status FROM tasks WHERE id = ?", (running,))["status"] == "running", "what is running finishes"
    tasks.recover()
    assert tasks.catch_up()["jobs_to_read"] == 0, "cancelled work isn't picked up again at the next start"
    assert db.q1("SELECT parse_status FROM jobs WHERE id = ?", (job_id,))["parse_status"] == "pending"

    tasks.enqueue("score_job", {"job_id": job_id}, ref=f"job:{job_id}")
    c = TestClient(app)
    r = c.post("/tasks/cancel-queued", follow_redirects=False)
    assert "Cancelled+2+waiting+tasks" in r.headers["location"]  # the recovered one and the new one
    assert "Cancel everything waiting" in c.get("/tasks").text
