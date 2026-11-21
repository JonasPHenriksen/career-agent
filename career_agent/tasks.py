"""Background work: a small persistent task queue, a worker, and a scheduler.

Everything slow (model calls, fetching) runs here so the UI stays responsive and work
survives restarts. Tasks are rows in the `tasks` table.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

from . import backup, db, history, settings

log = logging.getLogger("career_agent.tasks")

TASK_LABELS = {
    "analyze_document": "Reading document",
    "build_style": "Learning your writing style",
    "consolidate_facts": "Tidying up your career database",
    "parse_job": "Reading job ad",
    "score_job": "Scoring fit",
    "draft_application": "Writing application",
    "revise_application": "Revising application",
    "check_application": "Fact-checking application",
    "interview_prep": "Preparing interview notes",
    "followup": "Writing follow-up",
    "classify_response": "Reading employer's reply",
    "poll_source": "Checking job source",
    "collect_jobs": "Screening jobs you collected",
    "maintenance": "Housekeeping",
    "scan_folder": "Checking a watched folder",
    "embed_index": "Indexing documents for search",
    "pull_model": "Downloading the search model",
    "build_cv_base": "Reading your CV as the base for tailored CVs",
    "rescreen_jobs": "Screening job listings again",
    "check_cv_base": "Checking your base CV against your career database",
    "backup": "Backing up your data",
    "check_claude_models": "Checking which Claude models your plan offers",
    "place_answers": "Putting your answers in your career database",
    "read_voice": "Reading the letter for your voice",
}

_wake = threading.Event()


def enqueue(kind: str, payload: dict | None = None, ref: str | None = None, dedupe: bool = True) -> int:
    payload = payload or {}
    if dedupe:
        existing = db.q1("SELECT id FROM tasks WHERE kind = ? AND payload = ? AND status IN ('queued', 'running')",
                         (kind, json.dumps(payload, sort_keys=True)))
        if existing:
            return existing["id"]
    task_id = db.insert("tasks", {"kind": kind, "payload": json.dumps(payload, sort_keys=True), "ref": ref,
                                  "status": "queued", "created_at": db.now()})
    _wake.set()
    return task_id


def _handlers() -> dict:
    from . import collect, cvdata, jobs, profile, retrieval, review, sources, tracker, voice, watch, writer
    from .llm import plan as llm_plan

    def wait_for_analyses() -> None:
        """Let in-flight document analyses finish so we see everything."""
        for _ in range(300):
            if not db.scalar("SELECT COUNT(*) FROM tasks WHERE kind = 'analyze_document' AND status = 'running'"):
                return
            time.sleep(2)

    def consolidate(_: dict) -> dict:
        wait_for_analyses()
        return profile.consolidate()

    def build_style(payload: dict) -> dict:
        wait_for_analyses()
        voice.compute_baseline()
        if payload.get("auto") and settings.get("state").get("style_locked"):
            return {"skipped": "you edited the style guide yourself; rebuild it from Profile → Writing style"}
        voice.build_style_guide()
        settings.update("state", {"style_built_at": db.now()})
        return {"ok": True}

    def maintenance(_: dict) -> dict:
        from . import todos

        from . import collect

        result = tracker.maintenance()
        collect.remind()  # the weekly to-do to collect your saved searches, dated when one turns a week old
        result["reminders"] = todos.notify_due()  # dated to-dos whose day has come: one notification each
        return result

    return {
        "analyze_document": lambda p: _after_analysis(profile.analyze_document(p["doc_id"], replace=p.get("replace", False))),
        "build_style": build_style,
        "consolidate_facts": consolidate,
        "parse_job": lambda p: jobs.parse(p["job_id"], use_text=p.get("use_text", False)),
        "score_job": lambda p: jobs.score(p["job_id"]),
        "draft_application": lambda p: writer.draft(p["application_id"], p.get("part", "both")),
        "revise_application": lambda p: writer.revise(p["application_id"], p["instructions"], p.get("part", "letter")),
        "check_application": lambda p: writer.run_checks(p["application_id"]),
        "interview_prep": lambda p: writer.interview_prep(p["application_id"]),
        "followup": lambda p: writer.followup(p["application_id"]),
        "classify_response": lambda p: tracker.classify_response(p["application_id"], p["text"]),
        "poll_source": lambda p: sources.poll(p["source_id"]),
        "collect_jobs": collect.store,
        "maintenance": maintenance,
        "scan_folder": lambda p: watch.scan(p["folder_id"]),
        "embed_index": lambda p: retrieval.embed_pending(),
        "pull_model": lambda p: _pull_model(p["model"]),
        "build_cv_base": lambda p: _then_check_base(cvdata.build_base(p["doc_id"])),
        "check_cv_base": lambda p: review.check_base(p.get("base_id")),
        "place_answers": review.place_task,
        "read_voice": voice.read_task,
        "rescreen_jobs": lambda p: sources.rescreen(p.get("job_ids")),
        "backup": lambda p: _backup(),
        "check_claude_models": lambda p: llm_plan.check(),
    }


def _backup() -> dict:
    from . import notify

    try:
        return backup.run()
    except Exception as e:
        notify.send("Backup failed", str(e))
        raise


def _then_check_base(result: dict) -> dict:
    enqueue("check_cv_base", {"base_id": result.get("base_id")}, ref="cv-base-check")
    return result


def _pull_model(model: str) -> dict:
    import httpx2

    from . import retrieval

    url = (settings.get("llm").get("ollama_url") or "http://localhost:11434").rstrip("/")
    try:
        r = httpx2.post(f"{url}/api/pull", json={"model": model, "stream": False}, timeout=httpx2.Timeout(3600.0, connect=5.0))
    except httpx2.ConnectError as e:
        from .llm import LLMConfigError

        raise LLMConfigError("Ollama isn't running. Open the Ollama app and try again.") from e
    if r.status_code >= 400:
        raise RuntimeError(f"Ollama couldn't download {model}: {r.text[:200]}")
    retrieval.embeddings_available(force=True)
    enqueue("embed_index", {}, ref="index")
    return {"pulled": model}


def after_import() -> None:
    """Facts and documents arrived without the local model reading them (the agent folder's import):
    run the same follow-ups as after reading documents."""
    _after_analysis({})


def _after_analysis(result: dict) -> dict:
    """When no more documents are waiting, refresh the writing baseline and style guide."""
    queued = db.scalar("SELECT COUNT(*) FROM tasks WHERE kind = 'analyze_document' AND status = 'queued'")
    if not queued:
        enqueue("consolidate_facts", {}, ref="facts")
        enqueue("embed_index", {}, ref="index")
        # Rebuild the style guide only when a text you wrote yourself was (re)read since the last build.
        newest = db.scalar("SELECT MAX(analyzed_at) FROM documents WHERE authored = 1 AND status = 'analyzed' "
                           "AND kind IN ('application', 'email', 'report', 'other')")
        built = settings.get("state").get("style_built_at") or ""
        if newest and newest > built:
            enqueue("build_style", {"auto": True}, ref="style")
    return result


RETRY_MINUTES = (2, 10, 30)  # a passing model problem is tried again after these waits, then reported
# Failures that pass by themselves. Also used at start to find work an earlier session couldn't finish.
_TRANSIENT = re.compile(r"isn't running|disconnected|timed out|timeout|took too long to answer|couldn't load the page|"
                        r"connection (refused|reset|error|aborted)|couldn't (reach|connect)|temporarily|overloaded|"
                        r"rate.?limit|usage limit|slow down|\b(429|502|503|504|529)\b", re.I)


def is_passing(message: str | None) -> bool:
    return bool(message) and bool(_TRANSIENT.search(message))


def is_transient(e: Exception) -> bool:
    from .fetch import FetchError
    from .llm import LLMConfigError, LLMError, LLMTransient

    if isinstance(e, LLMTransient):
        return True
    if isinstance(e, FetchError):  # the site didn't answer: worth another try; a missing or blocked page isn't
        return bool(getattr(e, "connection", False) or getattr(e, "later", False))
    if isinstance(e, LLMConfigError):  # "Ollama isn't running" is passing; a missing key isn't
        return "isn't running" in str(e)
    return isinstance(e, LLMError) and is_passing(str(e))


# ---------------------------------------------------------------------------------------
# Waiting for what a task needs: a local model that isn't running, the internet, a plan's usage limit.
# The queue holds those tasks instead of using up their retries, and carries on when it's back.
# ---------------------------------------------------------------------------------------

# Which steps (llm.STEPS) each kind of task runs, so the queue knows which models it needs.
TASK_STEPS = {
    "draft_application": ("letter", "cv", "fact_check"), "revise_application": ("letter", "cv", "fact_check"),
    "check_application": ("fact_check",), "interview_prep": ("prep",), "followup": ("letter",), "build_style": ("style",),
    "build_cv_base": ("cv",), "check_cv_base": ("fact_check",), "rescreen_jobs": ("screen",), "parse_job": ("read_ad",), "score_job": ("score",), "poll_source": ("screen",), "collect_jobs": ("screen",),
    "analyze_document": ("read_document",), "consolidate_facts": ("read_document",), "classify_response": ("reply",),
    "place_answers": ("read_document",), "read_voice": ("fact_check",),
}
FETCH_TASKS = {"poll_source", "parse_job", "collect_jobs"}
NEED_LABELS = {"ollama": "Ollama (the local models)", "network": "an internet connection",
               "claude_code": "Claude (your plan's usage limit)"}

_holds: dict[str, dict] = {}  # what's unavailable -> {"why": text, "until": epoch seconds or None}
_holds_lock = threading.Lock()
_ollama_tried_at = 0.0


def hold(need: str, why: str, seconds: float | None = None) -> None:
    """Tasks that need this wait. With `seconds` the hold ends by itself; without, when it's checked and back."""
    with _holds_lock:
        _holds[need] = {"why": why, "until": time.time() + seconds if seconds else None}


def release(need: str) -> None:
    with _holds_lock:
        _holds.pop(need, None)


def holds() -> dict[str, str]:
    """What the queue is waiting for right now: {need: why}."""
    with _holds_lock:
        for need in [n for n, h in _holds.items() if h["until"] and h["until"] <= time.time()]:
            del _holds[need]
        return {n: h["why"] for n, h in _holds.items()}


def needs(task: dict) -> set[str]:
    """What a task can't run without: the provider behind its model tier, and the internet for fetching."""
    from . import llm

    kind = task["kind"]
    try:
        payload = json.loads(task.get("payload") or "{}")
    except ValueError:
        payload = {}
    out: set[str] = set()
    if kind in FETCH_TASKS and not payload.get("use_text"):
        out.add("network")
    if kind == "pull_model":
        return {"ollama", "network"}
    if kind == "check_claude_models":
        return {"claude_code"}
    for step in TASK_STEPS.get(kind, ()):
        provider = llm.step_provider(step, payload.get("model"))
        if provider:
            out.add(provider)
    return out


def start_ollama() -> bool:
    """Open the Ollama app when a task needs it and it isn't running (at most every two minutes)."""
    global _ollama_tried_at
    from .llm.providers import is_local_url

    if os.environ.get("CAREER_AGENT_NO_OLLAMA_START") or time.monotonic() - _ollama_tried_at < 120:
        return False
    if not is_local_url(settings.get("llm").get("ollama_url") or "http://localhost:11434"):
        return False
    _ollama_tried_at = time.monotonic()
    import shutil
    import subprocess

    log.info("A task needs Ollama and it isn't running: starting it")
    try:
        if os.path.isdir("/Applications/Ollama.app"):
            subprocess.Popen(["open", "-gj", "-a", "/Applications/Ollama.app"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        if shutil.which("ollama"):
            subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
            return True
    except OSError:
        pass
    return False


def unavailable(needed: set[str]) -> set[str]:
    """Which of these needs can't be met right now. Ollama is checked (and started) directly; the internet is
    checked again once a fetch has failed; usage limits end on their own clock."""
    from . import fetch, llm

    if "ollama" in needed:
        if llm.ollama_running():
            release("ollama")
        else:
            started = start_ollama()
            hold("ollama", "Ollama isn't running. " + ("Starting it…" if started else "Open the Ollama app."))
    if "network" in needed and "network" in holds() and fetch.online():
        release("network")
    return needed & set(holds())


def why_wait(e: Exception) -> tuple[str, int, str] | None:
    """If a task failed because something it needs is away for a while: (need, seconds to wait, why)."""
    from . import fetch
    from .llm import LLMConfigError, LLMTransient

    text = str(e)
    if isinstance(e, LLMConfigError) and "Ollama isn't running" in text:
        return "ollama", 30, "Ollama isn't running."
    if isinstance(e, LLMTransient) and (e.retry_after or 0) >= 600:  # a usage limit, not a blip
        return getattr(e, "provider", None) or "claude_code", int(e.retry_after), text
    if isinstance(e, fetch.FetchError) and getattr(e, "connection", False) and not fetch.online(force=True):
        return "network", 120, "This Mac is offline."
    return None


def run_task(task: dict) -> None:
    from . import llm

    handlers = _handlers()
    db.update("tasks", task["id"], {"status": "running", "started_at": db.now(), "error": None})
    try:
        fn = handlers.get(task["kind"])
        if fn is None:
            raise RuntimeError(f"Unknown task type {task['kind']}")
        payload = json.loads(task["payload"] or "{}")
        with history.op(TASK_LABELS.get(task["kind"], task["kind"])), llm.using(ref=task.get("ref"), model=payload.get("model")):
            result = fn(payload)
        db.update("tasks", task["id"], {"status": "done", "finished_at": db.now(),
                                        "progress": json.dumps(result, ensure_ascii=False)[:2000] if result else None})
    except Exception as e:  # noqa: BLE001 - every failure is recorded on the task
        from .fetch import FetchError
        from .llm import LLMError

        msg = str(e) if isinstance(e, (LLMError, FetchError)) else f"{e.__class__.__name__}: {e}"
        if not isinstance(e, (LLMError, FetchError)):
            log.error("Task %s failed:\n%s", task["id"], traceback.format_exc())
        attempts = int(task.get("attempts") or 0)
        waiting = why_wait(e)
        if waiting:  # what it needs is away: wait for it without using up a retry
            need, seconds, why = waiting
            hold(need, why, None if need in ("ollama", "network") else seconds)
            when = datetime.now(timezone.utc) + timedelta(seconds=seconds)
            db.update("tasks", task["id"], {"status": "queued", "started_at": None,
                                            "run_after": when.strftime("%Y-%m-%dT%H:%M:%S"),
                                            "error": f"Waiting for {NEED_LABELS.get(need, need)}: {why[:300]}"})
            return
        if is_transient(e) and attempts < len(RETRY_MINUTES):
            wait = getattr(e, "retry_after", None) or RETRY_MINUTES[attempts] * 60
            when = datetime.now(timezone.utc) + timedelta(seconds=wait)
            db.update("tasks", task["id"], {"status": "queued", "attempts": attempts + 1, "started_at": None,
                                            "run_after": when.strftime("%Y-%m-%dT%H:%M:%S"),
                                            "error": f"{msg[:1500]} · trying again at {when.astimezone():%H:%M}"})
            return
        db.update("tasks", task["id"], {"status": "error", "finished_at": db.now(), "error": msg[:2000]})
        if task["kind"] == "draft_application":
            app_id = json.loads(task["payload"]).get("application_id")
            db.ex("UPDATE applications SET status = 'draft' WHERE id = ? AND status = 'drafting' AND letter IS NOT NULL", (app_id,))


def concurrency() -> int:
    cfg = settings.get("llm")
    if int(cfg.get("concurrency") or 0) > 0:
        return int(cfg["concurrency"])
    chosen = [cfg.get(t) or {} for t in ("writing", "analysis")] + list((cfg.get("steps") or {}).values())
    local = any(c.get("provider") == "ollama" for c in chosen)  # a local model runs one thing at a time
    return 1 if local else 3


def recover() -> None:
    """Tasks that were running when the app stopped go back in the queue, and waits from the last session end:
    a new session tries everything right away."""
    db.ex("UPDATE tasks SET status = 'queued', started_at = NULL WHERE status = 'running'")
    db.ex("UPDATE tasks SET run_after = NULL WHERE status = 'queued'")
    db.ex("UPDATE applications SET status = 'draft' WHERE status = 'drafting' AND letter IS NOT NULL "
          "AND id NOT IN (SELECT CAST(substr(ref, 5) AS INTEGER) FROM tasks WHERE ref LIKE 'app:%' AND status = 'queued')")


def _last_task(kind: str, ref: str) -> dict | None:
    return db.q1("SELECT * FROM tasks WHERE kind = ? AND ref = ? ORDER BY id DESC LIMIT 1", (kind, ref))


def _again(kind: str, payload: dict, ref: str) -> bool:
    """Queue this work again unless it's already queued, or its last try failed for a lasting reason
    (those stay failed until you press Retry). Old failed tries of the same work are cleared."""
    last = _last_task(kind, ref)
    if last and last["status"] in ("queued", "running"):
        return False
    if last and last["status"] in ("error", "cancelled") and not is_passing(last["error"]):
        return False
    db.ex("DELETE FROM tasks WHERE kind = ? AND ref = ? AND status = 'error'", (kind, ref))
    enqueue(kind, payload, ref=ref)
    return True


def on_hold() -> bool:
    """Automatic work is on hold (Settings → Automation): job sources and watched folders aren't checked and
    unfinished work isn't picked up, so starting the app uses no model. What you start by hand still runs."""
    return bool(settings.get("automation").get("on_hold"))


def queue_due() -> int:
    """Queue the job sources and watched folders whose time has come. Nothing while automatic work is on hold."""
    from . import sources, watch

    if on_hold():
        return 0
    n = 0
    for src in sources.due():
        enqueue("poll_source", {"source_id": src["id"]}, ref=f"source:{src['id']}")
        n += 1
    for folder in watch.due():
        enqueue("scan_folder", {"folder_id": folder["id"]}, ref=f"watch:{folder['id']}")
        n += 1
    return n


def catch_up() -> dict:
    """At start: pick up what an earlier session couldn't finish because the model wasn't running, the Mac was
    offline or asleep, or a usage limit was reached. The app isn't always on, so this is how work gets done."""
    from . import profile

    found = {"jobs_to_read": 0, "jobs_to_score": 0, "documents": 0, "drafts": 0, "sources": 0}
    if on_hold():
        return found
    # Failures that a later try of the same work made good are history, not something to look at.
    db.ex("DELETE FROM tasks WHERE status = 'error' AND ref IS NOT NULL AND EXISTS (SELECT 1 FROM tasks t WHERE "
          "t.kind = tasks.kind AND t.ref = tasks.ref AND t.id > tasks.id AND t.status = 'done')")
    recent = db.days_ago(45)
    for j in db.q("SELECT id, url, parse_status, parse_error FROM jobs WHERE status IN ('new', 'shortlisted') "
                  "AND parse_status IN ('pending', 'parsing', 'error') AND created_at >= ?", (recent,)):
        if j["parse_status"] == "error" and not is_passing(j["parse_error"]):
            continue  # page gone, blocked, not a job ad: trying again won't help
        payload = {"job_id": j["id"]} if j["url"] else {"job_id": j["id"], "use_text": True}
        if _again("parse_job", payload, f"job:{j['id']}"):
            db.update("jobs", j["id"], {"parse_status": "pending", "parse_error": None})
            found["jobs_to_read"] += 1
    if settings.get("automation").get("auto_score", True) and profile.has_facts():
        today = datetime.now().date().isoformat()
        for j in db.q("SELECT id FROM jobs WHERE status IN ('new', 'shortlisted') AND parse_status = 'parsed' "
                      "AND fit_score IS NULL AND (deadline IS NULL OR deadline >= ?) AND created_at >= ?", (today, recent)):
            found["jobs_to_score"] += _again("score_job", {"job_id": j["id"]}, f"job:{j['id']}")
        # A job whose deadline passed while it waited isn't scored any more: its failed try is history too.
        db.ex("DELETE FROM tasks WHERE kind = 'score_job' AND status = 'error' AND ref IN "
              "(SELECT 'job:' || id FROM jobs WHERE fit_score IS NULL AND deadline < ?)", (today,))
    for d in db.q("SELECT id, status, error FROM documents WHERE status IN ('new', 'analyzing', 'error')"):
        if d["status"] == "error" and not is_passing(d["error"]):
            continue
        found["documents"] += _again("analyze_document", {"doc_id": d["id"]}, f"doc:{d['id']}")
    for a in db.q("SELECT id FROM applications WHERE status = 'drafting' AND letter IS NULL AND cv IS NULL"):
        found["drafts"] += _again("draft_application", {"application_id": a["id"]}, f"app:{a['id']}")
    for s in db.q("SELECT id, last_error FROM sources WHERE enabled = 1 AND last_status = 'error'"):
        if is_passing(s["last_error"]):  # checked again now, not after the usual wait
            db.update("sources", s["id"], {"last_run_at": None, "last_status": "checking again", "last_error": None})
            db.ex("DELETE FROM tasks WHERE kind = 'poll_source' AND ref = ? AND status = 'error'", (f"source:{s['id']}",))
            found["sources"] += 1
    if any(found.values()):
        log.info("Catching up on unfinished work: %s", found)
    return found


def queue_state() -> dict:
    """What the queue is doing, for the page header and `./agent run`."""
    now = db.now()
    running = db.scalar("SELECT COUNT(*) FROM tasks WHERE status = 'running'") or 0
    queued = db.q("SELECT run_after FROM tasks WHERE status = 'queued'")
    later = sorted(r["run_after"] for r in queued if r["run_after"] and r["run_after"] > now)
    waiting_for = holds()
    return {"running": running, "queued": len(queued) - len(later), "waiting": len(later),
            "next_at": later[0] if later else None, "busy": running + len(queued),
            "holds": {NEED_LABELS.get(n, n): why for n, why in waiting_for.items()}}


def comes_later(row: dict, screening: bool) -> bool:
    """Work that waits for other work in the queue, so nothing is done twice or on stale input: ads aren't read or
    scored while listings are being screened again (screening decides which are worth reading), and a draft isn't
    written while its job ad is being read or scored again."""
    if row["kind"] in ("parse_job", "score_job"):
        return screening
    if row["kind"] in ("draft_application", "check_application") and (row.get("ref") or "").startswith("app:"):
        return bool(db.q1(
            "SELECT 1 FROM tasks t JOIN applications a ON t.ref = 'job:' || a.job_id WHERE a.id = ? "
            "AND t.kind IN ('parse_job', 'score_job') AND t.status IN ('queued', 'running')", (row["ref"][4:],)))
    return False


async def worker_loop(stop: asyncio.Event) -> None:
    running: set[asyncio.Task] = set()
    while not stop.is_set():
        try:
            slots = concurrency() - len(running)
            rows = db.q("SELECT * FROM tasks WHERE status = 'queued' AND (run_after IS NULL OR run_after <= ?) "
                        "ORDER BY id LIMIT 200", (db.now(),)) if slots > 0 else []
            if rows:
                memo: dict[tuple, set[str]] = {}
                need = {}
                for row in rows:  # many tasks share a kind: work out what each kind needs once
                    key = (row["kind"], row["payload"] if '"model"' in row["payload"] or '"use_text"' in row["payload"] else "")
                    if key not in memo:
                        memo[key] = needs(row)
                    need[row["id"]] = memo[key]
                away = await asyncio.to_thread(unavailable, set().union(*need.values()))
                screening = bool(db.q1("SELECT 1 FROM tasks WHERE kind = 'rescreen_jobs' AND status IN ('queued', 'running')"))
                for row in rows:
                    if slots <= 0:
                        break
                    if need[row["id"]] & away:
                        continue  # stays queued until what it needs is back
                    if comes_later(row, screening):
                        continue
                    db.update("tasks", row["id"], {"status": "running", "started_at": db.now()})
                    t = asyncio.create_task(asyncio.to_thread(run_task, row))
                    running.add(t)
                    t.add_done_callback(running.discard)
                    slots -= 1
        except Exception:  # noqa: BLE001
            log.exception("worker loop error")
        _wake.clear()
        try:
            await asyncio.wait_for(asyncio.to_thread(_wake.wait, 2.0), timeout=3.0)
        except asyncio.TimeoutError:
            pass
    for t in running:
        t.cancel()


def _in_history(context: str, fn, *args):
    with history.op(context):
        return fn(*args)


async def scheduler_loop(stop: asyncio.Event) -> None:
    """Checks sources when due, marks ghosted applications, sends a morning digest, makes the weekly backup, and
    keeps the list of Claude models your plan offers up to date."""
    from . import tracker
    from .llm import plan as llm_plan

    while not stop.is_set():
        try:
            with history.op("Checking what's due"):
                queue_due()
            if backup.due():  # also while automatic work is on hold: it uses no model
                backup.mark_tried()
                enqueue("backup", {}, ref="backup")
            state = settings.get("state")
            now_local = datetime.now()
            last_maint = db.parse_ts(state.get("last_maintenance"))
            if not on_hold() and llm_plan.due():  # Claude Code updated, or a week since the plan's models were checked
                enqueue("check_claude_models", {}, ref="claude-models")
            if not last_maint or (db.parse_ts(db.now()) - last_maint).total_seconds() > 6 * 3600:
                enqueue("maintenance", {}, ref="maintenance")
                settings.update("state", {"last_maintenance": db.now()})
            today = now_local.date().isoformat()
            if now_local.hour >= 8 and state.get("last_digest") != today:
                settings.update("state", {"last_digest": today})
                await asyncio.to_thread(_in_history, "Morning digest", tracker.daily_digest)
        except Exception:  # noqa: BLE001
            log.exception("scheduler error")
        try:
            await asyncio.wait_for(stop.wait(), timeout=60)
        except asyncio.TimeoutError:
            pass


def active(ref: str | None = None) -> list[dict]:
    if ref:
        return db.q("SELECT * FROM tasks WHERE ref = ? AND status IN ('queued', 'running') ORDER BY id", (ref,))
    return db.q("SELECT * FROM tasks WHERE status IN ('queued', 'running') ORDER BY id")


def last_error(ref: str) -> dict | None:
    row = db.q1("SELECT * FROM tasks WHERE ref = ? ORDER BY id DESC LIMIT 1", (ref,))
    return row if row and row["status"] == "error" else None


def retry(task_id: int) -> None:
    db.ex("UPDATE tasks SET status = 'queued', error = NULL, started_at = NULL, finished_at = NULL WHERE id = ? "
          "AND status IN ('error', 'cancelled')", (task_id,))
    _wake.set()


def cancel_queued() -> int:
    """Drop everything that is waiting to run (what is running finishes). For stopping a big redo midway:
    cancelled work isn't picked up again at the next start."""
    n = db.scalar("SELECT COUNT(*) FROM tasks WHERE status = 'queued'") or 0
    db.ex("UPDATE tasks SET status = 'cancelled', finished_at = ? WHERE status = 'queued'", (db.now(),))
    return n


def cancel(task_id: int) -> None:
    db.ex("UPDATE tasks SET status = 'cancelled', finished_at = ? WHERE id = ? AND status = 'queued'", (db.now(), task_id))
