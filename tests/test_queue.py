"""The queue when things aren't available: a local model that isn't running, no internet, a plan's usage limit.
Tasks wait without using up their retries, and a new session picks up what an earlier one couldn't finish."""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from career_agent import db, fetch, jobs, llm, profile, settings, tasks
from career_agent.llm.errors import LLMConfigError, LLMError, LLMTransient
from career_agent.web.app import app


@pytest.fixture(autouse=True)
def no_holds():
    tasks._holds.clear()
    yield
    tasks._holds.clear()


def _task(kind: str, payload: str = "{}", ref: str | None = None, **more) -> int:
    return db.insert("tasks", {"kind": kind, "payload": payload, "ref": ref, "status": "queued",
                               "created_at": db.now(), **more})


def _run(tid: int) -> dict:
    tasks.run_task(db.q1("SELECT * FROM tasks WHERE id = ?", (tid,)))
    return db.q1("SELECT * FROM tasks WHERE id = ?", (tid,))


def _raises(e: Exception):
    def handler(payload):
        raise e
    return handler


def test_a_model_that_isnt_running_is_waited_for_without_using_retries(fake, monkeypatch):
    monkeypatch.setattr(tasks, "_handlers", lambda: {"x": _raises(
        LLMConfigError("Ollama isn't running. Open the Ollama app (or run `ollama serve`)."))})
    tid = _task("x")
    for _ in range(6):  # far more often than the three retries a passing error gets
        row = _run(tid)
        assert (row["status"], row["attempts"]) == ("queued", 0) and row["run_after"] > db.now()
        assert row["error"].startswith("Waiting for Ollama")
    assert "ollama" in tasks.holds()


def test_a_usage_limit_holds_that_provider_until_it_resets(fake, monkeypatch):
    limit = LLMTransient("Claude Code: Claude AI usage limit reached", retry_after=3600)
    limit.provider = "claude_code"
    blip = LLMTransient("Claude Code: overloaded, try again")  # no long wait: an ordinary retry
    monkeypatch.setattr(tasks, "_handlers", lambda: {"limit": _raises(limit), "blip": _raises(blip)})
    row = _run(_task("limit"))
    assert (row["status"], row["attempts"]) == ("queued", 0) and "usage limit" in row["error"]
    assert row["run_after"] > db.days_ago(-0.03)  # about an hour from now, not two minutes
    assert set(tasks.holds()) == {"claude_code"}
    row = _run(_task("blip"))
    assert (row["status"], row["attempts"]) == ("queued", 1) and set(tasks.holds()) == {"claude_code"}


def test_offline_waits_but_a_site_that_is_down_is_retried_then_reported(fake, monkeypatch):
    down = fetch.FetchError("Couldn't load the page (ConnectError).", connection=True)
    monkeypatch.setattr(tasks, "_handlers", lambda: {"down": _raises(down),
                                                     "gone": _raises(fetch.FetchError("Page not found."))})
    monkeypatch.setattr(fetch, "online", lambda force=False: False)
    row = _run(_task("down"))
    assert (row["status"], row["attempts"]) == ("queued", 0) and "offline" in row["error"]
    assert "network" in tasks.holds()

    monkeypatch.setattr(fetch, "online", lambda force=False: True)  # the Mac is online: it's the site
    tid = _task("down")
    for attempt in range(1, len(tasks.RETRY_MINUTES) + 1):
        assert _run(tid)["attempts"] == attempt
    assert _run(tid)["status"] == "error"
    assert _run(_task("gone"))["status"] == "error"  # a missing page isn't worth another try


def test_what_each_task_needs(fake):
    llm.set_test_provider(None)  # look at Settings, as the real queue does
    settings.update("llm", {"writing": {"provider": "claude_code", "model": "opus"},
                            "analysis": {"provider": "ollama", "model": "gemma4:26b-mlx"}, "check_with": "auto"})

    def needs(kind, payload="{}"):
        return tasks.needs({"kind": kind, "payload": payload})

    assert needs("score_job") == {"ollama"}
    assert needs("parse_job") == {"ollama", "network"}
    assert needs("parse_job", '{"job_id": 1, "use_text": true}') == {"ollama"}
    assert needs("poll_source") == {"ollama", "network"}
    assert needs("draft_application") == {"claude_code"}  # written and fact-checked by the cloud writer
    assert needs("score_job", '{"job_id": 1, "model": "claude_code|sonnet"}') == {"claude_code"}
    assert needs("maintenance") == set() and needs("embed_index") == set()
    settings.update("llm", {"check_with": "analysis"})
    assert needs("draft_application") == {"claude_code", "ollama"}


def test_ollama_is_checked_live_and_released_when_it_is_back(fake, monkeypatch):
    monkeypatch.setattr(llm, "ollama_running", lambda: False)
    assert tasks.unavailable({"ollama", "network"}) == {"ollama"}
    assert tasks.queue_state()["holds"] == {"Ollama (the local models)": "Ollama isn't running. Open the Ollama app."}
    monkeypatch.setattr(llm, "ollama_running", lambda: True)
    assert tasks.unavailable({"ollama", "network"}) == set() and not tasks.holds()


def test_the_worker_leaves_held_tasks_queued_and_runs_the_rest(fake, monkeypatch):
    ran = []
    monkeypatch.setattr(tasks, "_handlers", lambda: {"score_job": lambda p: ran.append("score"),
                                                     "maintenance": lambda p: ran.append("maintenance")})
    tasks.hold("fake", "The model is away.")  # the test provider is called "fake"
    held, free = _task("score_job"), _task("maintenance")

    async def spin():
        stop = asyncio.Event()
        worker = asyncio.create_task(tasks.worker_loop(stop))
        for _ in range(40):
            await asyncio.sleep(0.05)
            if db.q1("SELECT status FROM tasks WHERE id = ?", (free,))["status"] == "done":
                break
        stop.set()
        tasks._wake.set()
        await worker

    asyncio.run(spin())
    assert ran == ["maintenance"]
    assert db.q1("SELECT status FROM tasks WHERE id = ?", (held,))["status"] == "queued"
    state = tasks.queue_state()
    assert state["busy"] == 1 and state["queued"] == 1 and state["holds"] == {"fake": "The model is away."}


def test_a_new_session_picks_up_what_the_last_one_could_not_finish(fake):
    profile.save_facts(None, [{"category": "skill", "title": "Scrum"}])
    down = "Ollama isn't running. Open the Ollama app (or run `ollama serve`)."

    def job(n, **cols):
        return db.insert("jobs", {"url": f"https://example.com/{n}", "status": "new", "created_at": db.now(), **cols})

    unread = job(1, parse_status="error", parse_error=down)
    gone = job(2, parse_status="error", parse_error="Page not found. The ad may have been taken down.")
    unscored = job(3, parse_status="parsed")
    bad_score = job(4, parse_status="parsed")
    scored = job(5, parse_status="parsed", fit_score=70)
    expired = job(6, parse_status="parsed", deadline="2020-01-01")
    old = [_task("parse_job", '{"job_id": 1}', "job:1", status="error", error=down),
           _task("score_job", '{"job_id": 3}', "job:3", status="error", error=down)]
    _task("score_job", '{"job_id": 4}', "job:4", status="error", error="The model's answer wasn't valid JSON, even after a retry.")
    too_late = _task("score_job", '{"job_id": 6}', "job:6", status="error", error=down)
    sid = db.insert("sources", {"name": "s", "kind": "rss", "url": "https://example.com/rss", "created_at": db.now(),
                                "last_run_at": db.now(), "last_status": "error",
                                "last_error": "Couldn't load the page (ConnectError)."})
    waiting = _task("score_job", '{"job_id": 5}', "job:5", run_after="2999-01-01T00:00:00", status="running")
    made_good = _task("poll_source", '{"source_id": 9}', "source:9", status="error", error="Couldn't load the page (ConnectError).")
    _task("poll_source", '{"source_id": 9}', "source:9", status="done")

    tasks.recover()
    found = tasks.catch_up()

    assert found == {"jobs_to_read": 1, "jobs_to_score": 1, "documents": 0, "drafts": 0, "sources": 1}
    queued = {(t["kind"], t["ref"]) for t in db.q("SELECT kind, ref FROM tasks WHERE status = 'queued'")}
    assert queued == {("parse_job", f"job:{unread}"), ("score_job", f"job:{unscored}"), ("score_job", f"job:{scored}")}
    assert db.q1("SELECT parse_status, parse_error FROM jobs WHERE id = ?", (unread,)) == {"parse_status": "pending", "parse_error": None}
    assert db.q1("SELECT parse_status FROM jobs WHERE id = ?", (gone,))["parse_status"] == "error"
    assert not db.q("SELECT 1 FROM tasks WHERE status = 'queued' AND ref IN (?, ?)", (f"job:{bad_score}", f"job:{expired}"))
    assert not db.q(f"SELECT 1 FROM tasks WHERE id IN ({old[0]}, {old[1]})"), "old failed tries are cleared"
    assert not db.q1("SELECT 1 FROM tasks WHERE id = ?", (made_good,)), "a failure that a later try made good is cleared"
    assert not db.q1("SELECT 1 FROM tasks WHERE id = ?", (too_late,)), "an expired job's failed scoring is cleared"
    assert db.q1("SELECT last_run_at FROM sources WHERE id = ?", (sid,))["last_run_at"] is None
    assert db.q1("SELECT status, run_after FROM tasks WHERE id = ?", (waiting,)) == {"status": "queued", "run_after": None}
    assert tasks.catch_up() == {"jobs_to_read": 0, "jobs_to_score": 0, "documents": 0, "drafts": 0, "sources": 0}


def test_on_hold_nothing_starts_on_its_own(fake):
    job = db.insert("jobs", {"title": "Unread", "url": "https://example.com/a", "status": "new", "parse_status": "pending",
                             "created_at": db.now(), "updated_at": db.now()})
    sid = db.insert("sources", {"name": "Feed", "kind": "rss", "url": "https://example.com/feed", "created_at": db.now()})
    settings.update("automation", {"on_hold": True})
    try:
        assert tasks.catch_up() == {"jobs_to_read": 0, "jobs_to_score": 0, "documents": 0, "drafts": 0, "sources": 0}
        assert tasks.queue_due() == 0
        assert not tasks.active()
        page = TestClient(app).get("/jobs")
        assert "Automatic work is on hold" in page.text

        # Resume (the banner's button): what was waiting is picked up at once.
        TestClient(app).post("/settings/hold", data={"on": "0"}, follow_redirects=False)
        assert not tasks.on_hold()
        queued = {(x["kind"], x["ref"]) for x in tasks.active()}
        assert queued == {("parse_job", f"job:{job}"), ("poll_source", f"source:{sid}")}
        assert "Automatic work is on hold" not in TestClient(app).get("/jobs").text
    finally:
        settings.update("automation", {"on_hold": False})
        db.ex("DELETE FROM tasks")
        db.ex("DELETE FROM sources WHERE id = ?", (sid,))
        db.ex("DELETE FROM jobs WHERE id = ?", (job,))


def test_screening_comes_before_reading_and_reading_before_drafting(fake):
    sid = db.insert("sources", {"name": "Feed", "kind": "rss", "url": "https://example.com/feed2", "created_at": db.now()})
    job = db.insert("jobs", {"title": "Backend developer", "url": "https://example.com/b", "status": "new",
                             "parse_status": "parsed", "source_id": sid, "created_at": db.now(), "updated_at": db.now()})
    app_id = db.insert("applications", {"job_id": job, "status": "draft", "created_at": db.now(), "updated_at": db.now()})
    other = db.insert("jobs", {"title": "Tester", "url": "https://example.com/c", "status": "filtered",
                               "parse_status": "parsed", "source_id": sid, "created_at": db.now(), "updated_at": db.now()})
    try:
        read = db.q1("SELECT * FROM tasks WHERE id = ?", (_task("parse_job", '{"job_id": %d}' % job, f"job:{job}"),))
        draft = db.q1("SELECT * FROM tasks WHERE id = ?", (_task("draft_application", "{}", f"app:{app_id}"),))
        assert tasks.comes_later(read, screening=True) and not tasks.comes_later(read, screening=False)
        assert tasks.comes_later(draft, screening=False), "its ad is about to be read again"
        db.update("tasks", read["id"], {"status": "done"})
        assert not tasks.comes_later(draft, screening=False)

        # A listing that was screened out after its reading was queued isn't fetched or read.
        calls = len(fake.calls)
        assert jobs.parse(other) == {"skipped": "set aside"} and len(fake.calls) == calls
    finally:
        db.ex("DELETE FROM tasks")
        db.ex("DELETE FROM applications WHERE id = ?", (app_id,))
        db.ex("DELETE FROM jobs WHERE id IN (?, ?)", (job, other))
        db.ex("DELETE FROM sources WHERE id = ?", (sid,))


def test_errors_say_which_provider_failed(fake, monkeypatch):
    monkeypatch.setattr(fake, "complete", _raises(LLMTransient("usage limit reached", retry_after=3600)))
    with pytest.raises(LLMError) as caught:
        llm.run("analysis", "test", "system", "user")
    assert caught.value.provider == "fake"


def test_queue_endpoint_and_indicator(fake):
    client = TestClient(app)
    assert client.get("/api/queue").json() == {"running": 0, "queued": 0, "waiting": 0, "next_at": None,
                                               "busy": 0, "holds": {}}
    _task("score_job")
    tasks.hold("ollama", "Ollama isn't running.")
    assert client.get("/api/queue").json()["busy"] == 1
    assert "Waiting for Ollama (the local models) · 1 task<" in client.get("/tasks/indicator").text
